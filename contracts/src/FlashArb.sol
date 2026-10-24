// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Ownable, Ownable2Step} from "@openzeppelin/contracts/access/Ownable2Step.sol";
import {Pausable} from "@openzeppelin/contracts/utils/Pausable.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";

import {IAavePoolMinimal} from "./interfaces/IAavePoolMinimal.sol";
import {IFlashLoanSimpleReceiverMinimal} from "./interfaces/IFlashLoanSimpleReceiverMinimal.sol";
import {IV3PoolMinimal} from "./interfaces/IV3PoolMinimal.sol";
import {IV2PairMinimal} from "./interfaces/IV2PairMinimal.sol";
import {ISolidlyPairMinimal} from "./interfaces/ISolidlyPairMinimal.sol";

enum Venue {
    UNI_V3,
    PANCAKE_V3,
    UNI_V2,
    SOLIDLY_V2
}

struct Hop {
    address pool; // whitelisted pool or pair
    address tokenIn;
    address tokenOut;
    Venue venue; // must equal the whitelist entry
    bool zeroForOne; // must equal (tokenIn == pool.token0)
}

struct Route {
    address loanToken; // flash-borrowed token; profit is measured in it
    uint256 loanAmount; // raw units; input of hop 0
    uint256 minProfit; // raw units of loanToken, net of the Aave premium
    uint64 deadline; // unix seconds; require block.timestamp <= deadline
    Hop[] hops; // 2..MAX_HOPS
}

struct PoolInfo {
    address token0;
    address token1;
    Venue venue;
    uint16 feeBps; // UNI_V2 only (1..100); must be 0 for other venues
    bool enabled;
}

/// @title FlashArb v1 (simulation only)
/// @notice Aave v3 flashLoanSimple + direct swaps on whitelisted pools/pairs. No routers, no DEX approvals.
/// @dev Design: docs/FLASH_ARB_DESIGN.md section 3. Never deployed to a real network in this plan.
contract FlashArb is IFlashLoanSimpleReceiverMinimal, Ownable2Step, Pausable, ReentrancyGuard {
    using SafeERC20 for IERC20;

    // ---------------------------------------------------------------- constants
    uint256 public constant MAX_HOPS = 4;
    uint160 internal constant MIN_SQRT_RATIO = 4295128739; // from Uniswap TickMath
    uint160 internal constant MAX_SQRT_RATIO = 1461446703485210103287273052203988822378723970342; // from Uniswap TickMath
    uint256 internal constant LOAN_STEP = type(uint256).max; // "hop index" used in errors for the loan receipt

    address public immutable AAVE_POOL;

    // ---------------------------------------------------------------- config storage
    mapping(address => bool) public allowedToken;
    mapping(address => uint256) public maxLoan; // 0 = cannot be borrowed
    mapping(address => PoolInfo) public pools;

    // ---------------------------------------------------------------- in-flight state (all zero outside executeArb)
    // SPEC-DEVIATION: the spec says `private`; these are `internal` only so that the test-only subclass
    // FlashArbExposed can expose getters for invariant I3. No external difference, no production impact.
    struct Pending {
        address pool;
        bool zeroForOne;
    }

    bytes32 internal _routeHash; // keccak256(params) of the active loan
    Pending internal _pending; // set only around one v3 swap() call
    uint256 internal _pendingMaxPay; // that hop's amountIn
    uint256 internal _lastPremium; // premium of the active loan, for the event

    // ---------------------------------------------------------------- events
    event ArbExecuted(
        bytes32 indexed routeHash,
        address indexed loanToken,
        uint256 loanAmount,
        uint256 premium,
        uint256 profit
    );
    event HopExecuted(uint256 indexed index, address indexed pool, uint256 amountIn, uint256 amountOut);
    event TokenSet(address indexed token, bool allowed, uint256 maxLoan);
    event PoolSet(address indexed pool, Venue venue, address token0, address token1, uint16 feeBps, bool enabled);
    event Swept(address indexed token, address indexed to, uint256 amount);

    // ---------------------------------------------------------------- errors
    error NotAContract(address account);
    error RenounceDisabled();
    error InvalidPoolConfig(address pool);
    error NotAavePool();
    error BadInitiator(address initiator);
    error NoActiveRoute();
    error RouteHashMismatch();
    error LoanMismatch();
    error BadHopCount(uint256 n);
    error RouteNotClosed();
    error HopChainBroken(uint256 hop);
    error PoolNotWhitelisted(address pool);
    error VenueMismatch(uint256 hop);
    error PoolTokenMismatch(uint256 hop);
    error DirectionMismatch(uint256 hop);
    error DuplicatePool(address pool);
    error TokenNotAllowed(address token);
    error LoanTooLarge(uint256 amount, uint256 cap);
    error ZeroAmount();
    error AmountTooLarge();
    error DeadlineExpired(uint256 deadline, uint256 nowTs);
    error UnexpectedCallback(address caller);
    error BadCallbackAmount(int256 amount0Delta, int256 amount1Delta);
    error PartialFill(uint256 hop, uint256 paid, uint256 amountIn);
    error ZeroOutput(uint256 hop);
    error BalanceDeltaMismatch(uint256 hop, address token, uint256 expected, uint256 actual);
    error InsufficientProfit(uint256 balance, uint256 required);

    // ---------------------------------------------------------------- constructor / admin
    constructor(address aavePool, address initialOwner) Ownable(initialOwner) {
        if (aavePool.code.length == 0) revert NotAContract(aavePool);
        AAVE_POOL = aavePool;
    }

    /// @notice Disabled: a lost owner would make the contract unusable and config unreachable.
    function renounceOwnership() public view override onlyOwner {
        revert RenounceDisabled();
    }

    function pause() external onlyOwner {
        _pause();
    }

    function unpause() external onlyOwner {
        _unpause();
    }

    function setToken(address token, bool allowed, uint256 maxLoanAmount) external onlyOwner {
        if (token.code.length == 0) revert NotAContract(token);
        // SPEC-DEVIATION: the spec names no error for "maxLoanAmount > 0 requires allowed";
        // TokenNotAllowed(token) is the closest exact-named error.
        if (maxLoanAmount > 0 && !allowed) revert TokenNotAllowed(token);
        allowedToken[token] = allowed;
        maxLoan[token] = maxLoanAmount;
        emit TokenSet(token, allowed, maxLoanAmount);
    }

    function setPool(address pool, Venue venue, uint16 feeBps, bool enabled) external onlyOwner {
        PoolInfo storage p = pools[pool];
        if (!enabled) {
            p.enabled = false;
            emit PoolSet(pool, p.venue, p.token0, p.token1, p.feeBps, false);
            return;
        }
        if (pool.code.length == 0) revert NotAContract(pool);
        address t0 = IV3PoolMinimal(pool).token0();
        address t1 = IV3PoolMinimal(pool).token1();
        if (!allowedToken[t0]) revert TokenNotAllowed(t0);
        if (!allowedToken[t1]) revert TokenNotAllowed(t1);
        if (venue == Venue.UNI_V2) {
            if (feeBps < 1 || feeBps > 100) revert InvalidPoolConfig(pool);
        } else if (feeBps != 0) {
            revert InvalidPoolConfig(pool);
        }
        pools[pool] = PoolInfo({token0: t0, token1: t1, venue: venue, feeBps: feeBps, enabled: true});
        emit PoolSet(pool, venue, t0, t1, feeBps, true);
    }

    function sweep(address token, uint256 amount) external onlyOwner nonReentrant {
        if (amount == type(uint256).max) amount = IERC20(token).balanceOf(address(this));
        address to = owner();
        IERC20(token).safeTransfer(to, amount);
        emit Swept(token, to, amount);
    }

    // ---------------------------------------------------------------- executeArb
    function executeArb(
        Route calldata route
    ) external onlyOwner whenNotPaused nonReentrant returns (uint256 profit) {
        _validateRoute(route);

        address loanToken = route.loanToken;
        uint256 balBefore = IERC20(loanToken).balanceOf(address(this));
        bytes memory params = abi.encode(balBefore, route);
        bytes32 routeHash = keccak256(params);
        _routeHash = routeHash;

        IAavePoolMinimal(AAVE_POOL).flashLoanSimple(address(this), loanToken, route.loanAmount, params, 0);

        _routeHash = 0;
        if (IERC20(loanToken).allowance(address(this), AAVE_POOL) != 0) {
            IERC20(loanToken).forceApprove(AAVE_POOL, 0);
        }

        uint256 balAfter = IERC20(loanToken).balanceOf(address(this));
        uint256 required = balBefore + route.minProfit;
        if (balAfter < required) revert InsufficientProfit(balAfter, required);
        profit = balAfter - balBefore;
        if (profit > 0) IERC20(loanToken).safeTransfer(owner(), profit);

        uint256 premium = _lastPremium;
        _lastPremium = 0;
        emit ArbExecuted(routeHash, loanToken, route.loanAmount, premium, profit);
    }

    function _validateRoute(Route calldata route) internal view {
        uint256 n = route.hops.length;
        if (n < 2 || n > MAX_HOPS) revert BadHopCount(n);
        uint256 amount = route.loanAmount;
        if (amount == 0) revert ZeroAmount();
        uint256 cap = maxLoan[route.loanToken];
        if (amount > cap) revert LoanTooLarge(amount, cap);
        if (amount > uint256(type(int256).max)) revert AmountTooLarge();
        if (block.timestamp > route.deadline) revert DeadlineExpired(route.deadline, block.timestamp);
        if (route.hops[0].tokenIn != route.loanToken || route.hops[n - 1].tokenOut != route.loanToken) {
            revert RouteNotClosed();
        }
        for (uint256 i = 0; i < n; i++) {
            Hop calldata h = route.hops[i];
            _checkHop(i, h);
            if (i > 0 && h.tokenIn != route.hops[i - 1].tokenOut) revert HopChainBroken(i);
            for (uint256 j = 0; j < i; j++) {
                if (route.hops[j].pool == h.pool) revert DuplicatePool(h.pool);
            }
        }
    }

    /// @dev Whitelist / token / direction checks of one hop (shared with the test-only subclass).
    function _checkHop(uint256 i, Hop calldata h) internal view {
        PoolInfo storage p = pools[h.pool];
        if (!p.enabled) revert PoolNotWhitelisted(h.pool);
        if (h.venue != p.venue) revert VenueMismatch(i);
        address tin = h.tokenIn;
        address tout = h.tokenOut;
        if (tin == tout || !((tin == p.token0 && tout == p.token1) || (tin == p.token1 && tout == p.token0))) {
            revert PoolTokenMismatch(i);
        }
        if (h.zeroForOne != (tin == p.token0)) revert DirectionMismatch(i);
        if (!allowedToken[tin]) revert TokenNotAllowed(tin);
        if (!allowedToken[tout]) revert TokenNotAllowed(tout);
    }

    // ---------------------------------------------------------------- Aave callback
    function executeOperation(
        address asset,
        uint256 amount,
        uint256 premium,
        address initiator,
        bytes calldata params
    ) external returns (bool) {
        if (msg.sender != AAVE_POOL) revert NotAavePool();
        if (initiator != address(this)) revert BadInitiator(initiator);
        bytes32 rh = _routeHash;
        if (rh == bytes32(0)) revert NoActiveRoute();
        if (keccak256(params) != rh) revert RouteHashMismatch();

        _runRoute(asset, amount, premium, params);

        IERC20(asset).forceApprove(AAVE_POOL, amount + premium);
        return true;
    }

    function _runRoute(address asset, uint256 amount, uint256 premium, bytes calldata params) internal {
        (uint256 balBefore, Route memory route) = abi.decode(params, (uint256, Route));
        if (asset != route.loanToken || amount != route.loanAmount) revert LoanMismatch();

        uint256 loanBal = IERC20(asset).balanceOf(address(this));
        if (loanBal != balBefore + amount) {
            revert BalanceDeltaMismatch(LOAN_STEP, asset, balBefore + amount, loanBal);
        }

        uint256 x = amount;
        for (uint256 i = 0; i < route.hops.length; i++) {
            x = _swap(i, route.hops[i], x);
        }

        uint256 finalBal = IERC20(asset).balanceOf(address(this));
        uint256 required = balBefore + amount + premium + route.minProfit;
        if (finalBal < required) revert InsufficientProfit(finalBal, required);
        _lastPremium = premium;
    }

    // ---------------------------------------------------------------- swap step
    function _swap(uint256 i, Hop memory h, uint256 amountIn) internal returns (uint256 amountOut) {
        uint256 inBefore = IERC20(h.tokenIn).balanceOf(address(this));
        uint256 outBefore = IERC20(h.tokenOut).balanceOf(address(this));

        if (h.venue == Venue.UNI_V3 || h.venue == Venue.PANCAKE_V3) {
            amountOut = _swapV3(i, h, amountIn);
        } else if (h.venue == Venue.UNI_V2) {
            amountOut = _swapV2(i, h, amountIn);
        } else {
            amountOut = _swapSolidly(i, h, amountIn);
        }

        uint256 outAfter = IERC20(h.tokenOut).balanceOf(address(this));
        uint256 gotOut = outAfter >= outBefore ? outAfter - outBefore : 0;
        if (gotOut != amountOut) revert BalanceDeltaMismatch(i, h.tokenOut, amountOut, gotOut);
        uint256 inAfter = IERC20(h.tokenIn).balanceOf(address(this));
        uint256 spent = inBefore >= inAfter ? inBefore - inAfter : 0;
        if (spent != amountIn) revert BalanceDeltaMismatch(i, h.tokenIn, amountIn, spent);

        emit HopExecuted(i, h.pool, amountIn, amountOut);
    }

    function _swapV3(uint256 i, Hop memory h, uint256 amountIn) internal returns (uint256 amountOut) {
        if (amountIn > uint256(type(int256).max)) revert AmountTooLarge();
        _pending = Pending({pool: h.pool, zeroForOne: h.zeroForOne});
        _pendingMaxPay = amountIn;
        (int256 a0, int256 a1) = IV3PoolMinimal(h.pool).swap(
            address(this),
            h.zeroForOne,
            int256(amountIn),
            h.zeroForOne ? MIN_SQRT_RATIO + 1 : MAX_SQRT_RATIO - 1,
            ""
        );
        if (_pending.pool != address(0)) revert UnexpectedCallback(h.pool);
        _pendingMaxPay = 0;

        int256 paidDelta = h.zeroForOne ? a0 : a1;
        int256 outDelta = h.zeroForOne ? a1 : a0;
        // paidDelta <= 0 can never equal amountIn > 0 -> PartialFill; outDelta >= 0 means no output.
        uint256 paid = paidDelta > 0 ? uint256(paidDelta) : 0;
        if (paid != amountIn) revert PartialFill(i, paid, amountIn);
        if (outDelta >= 0) revert ZeroOutput(i);
        amountOut = uint256(-outDelta);
    }

    function _swapV2(uint256 i, Hop memory h, uint256 amountIn) internal returns (uint256 amountOut) {
        (uint112 r0, uint112 r1, ) = IV2PairMinimal(h.pool).getReserves();
        (uint256 rIn, uint256 rOut) = h.zeroForOne ? (uint256(r0), uint256(r1)) : (uint256(r1), uint256(r0));
        uint256 g = 10000 - uint256(pools[h.pool].feeBps);
        uint256 inWithFee = amountIn * g;
        amountOut = (inWithFee * rOut) / (rIn * 10000 + inWithFee);
        if (amountOut == 0) revert ZeroOutput(i);
        IERC20(h.tokenIn).safeTransfer(h.pool, amountIn);
        IV2PairMinimal(h.pool).swap(h.zeroForOne ? 0 : amountOut, h.zeroForOne ? amountOut : 0, address(this), "");
    }

    function _swapSolidly(uint256 i, Hop memory h, uint256 amountIn) internal returns (uint256 amountOut) {
        IERC20(h.tokenIn).safeTransfer(h.pool, amountIn);
        amountOut = ISolidlyPairMinimal(h.pool).getAmountOut(amountIn, h.tokenIn);
        if (amountOut == 0) revert ZeroOutput(i);
        ISolidlyPairMinimal(h.pool).swap(h.zeroForOne ? 0 : amountOut, h.zeroForOne ? amountOut : 0, address(this), "");
    }

    // ---------------------------------------------------------------- v3 callbacks
    function uniswapV3SwapCallback(int256 amount0Delta, int256 amount1Delta, bytes calldata) external {
        _onV3Callback(amount0Delta, amount1Delta);
    }

    function pancakeV3SwapCallback(int256 amount0Delta, int256 amount1Delta, bytes calldata) external {
        _onV3Callback(amount0Delta, amount1Delta);
    }

    function _onV3Callback(int256 a0, int256 a1) internal {
        Pending memory p = _pending;
        if (p.pool == address(0) || msg.sender != p.pool) revert UnexpectedCallback(msg.sender);
        int256 owed = p.zeroForOne ? a0 : a1;
        if (owed <= 0 || uint256(owed) > _pendingMaxPay) revert BadCallbackAmount(a0, a1);
        PoolInfo storage info = pools[p.pool];
        address token = p.zeroForOne ? info.token0 : info.token1;
        delete _pending; // effects before interaction
        IERC20(token).safeTransfer(msg.sender, uint256(owed));
    }
}
