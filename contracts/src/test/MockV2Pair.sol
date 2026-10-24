// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";

/// @notice TEST ONLY. Uniswap v2 pair: constant product, 0.3% fee, K check on balances.
contract MockV2Pair {
    using SafeERC20 for IERC20;

    address public token0;
    address public token1;
    uint112 private r0;
    uint112 private r1;

    constructor(address t0, address t1) {
        token0 = t0;
        token1 = t1;
    }

    function getReserves() external view returns (uint112, uint112, uint32) {
        return (r0, r1, 0);
    }

    /// @dev tokens must be transferred in first
    function sync() public {
        r0 = uint112(IERC20(token0).balanceOf(address(this)));
        r1 = uint112(IERC20(token1).balanceOf(address(this)));
    }

    function swap(uint256 a0Out, uint256 a1Out, address to, bytes calldata) external {
        require(a0Out > 0 || a1Out > 0, "INSUFFICIENT_OUTPUT");
        require(a0Out < r0 && a1Out < r1, "INSUFFICIENT_LIQUIDITY");
        if (a0Out > 0) IERC20(token0).safeTransfer(to, a0Out);
        if (a1Out > 0) IERC20(token1).safeTransfer(to, a1Out);
        uint256 b0 = IERC20(token0).balanceOf(address(this));
        uint256 b1 = IERC20(token1).balanceOf(address(this));
        uint256 a0In = b0 > r0 - a0Out ? b0 - (r0 - a0Out) : 0;
        uint256 a1In = b1 > r1 - a1Out ? b1 - (r1 - a1Out) : 0;
        require(a0In > 0 || a1In > 0, "INSUFFICIENT_INPUT");
        uint256 adj0 = b0 * 1000 - a0In * 3;
        uint256 adj1 = b1 * 1000 - a1In * 3;
        require(adj0 * adj1 >= uint256(r0) * uint256(r1) * 1e6, "K");
        r0 = uint112(b0);
        r1 = uint112(b1);
    }
}
