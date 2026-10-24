// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";

/// @notice TEST ONLY. Solidly style volatile pair: fee taken from the input before the curve (Aerodrome style).
contract MockSolidlyPair {
    using SafeERC20 for IERC20;

    address public token0;
    address public token1;
    uint256 public reserve0;
    uint256 public reserve1;
    uint256 public feeBps = 30;

    constructor(address t0, address t1) {
        token0 = t0;
        token1 = t1;
    }

    function setFeeBps(uint256 f) external {
        feeBps = f;
    }

    function sync() public {
        reserve0 = IERC20(token0).balanceOf(address(this));
        reserve1 = IERC20(token1).balanceOf(address(this));
    }

    function getAmountOut(uint256 amountIn, address tokenIn) external view returns (uint256) {
        uint256 a2 = amountIn - (amountIn * feeBps) / 10000;
        (uint256 rIn, uint256 rOut) = tokenIn == token0 ? (reserve0, reserve1) : (reserve1, reserve0);
        return (a2 * rOut) / (rIn + a2);
    }

    function swap(uint256 a0Out, uint256 a1Out, address to, bytes calldata) external {
        require(a0Out > 0 || a1Out > 0, "INSUFFICIENT_OUTPUT");
        require(a0Out < reserve0 && a1Out < reserve1, "INSUFFICIENT_LIQUIDITY");
        if (a0Out > 0) IERC20(token0).safeTransfer(to, a0Out);
        if (a1Out > 0) IERC20(token1).safeTransfer(to, a1Out);
        uint256 b0 = IERC20(token0).balanceOf(address(this));
        uint256 b1 = IERC20(token1).balanceOf(address(this));
        uint256 a0In = b0 > reserve0 - a0Out ? b0 - (reserve0 - a0Out) : 0;
        uint256 a1In = b1 > reserve1 - a1Out ? b1 - (reserve1 - a1Out) : 0;
        require(a0In > 0 || a1In > 0, "INSUFFICIENT_INPUT");
        // fee on the input is floored, exactly like getAmountOut (Aerodrome style)
        uint256 adj0 = b0 - (a0In * feeBps) / 10000;
        uint256 adj1 = b1 - (a1In * feeBps) / 10000;
        require(adj0 * adj1 >= reserve0 * reserve1, "K");
        reserve0 = b0;
        reserve1 = b1;
    }
}
