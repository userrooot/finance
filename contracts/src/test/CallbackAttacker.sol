// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {FlashArb} from "../FlashArb.sol";
import {MockAavePool} from "./MockAavePool.sol";

/// @notice TEST ONLY. Calls FlashArb's callbacks directly and starts loans naming FlashArb as receiver.
contract CallbackAttacker {
    function callUni(address arb, int256 a0, int256 a1) external {
        FlashArb(arb).uniswapV3SwapCallback(a0, a1, "");
    }

    function callPancake(address arb, int256 a0, int256 a1) external {
        FlashArb(arb).pancakeV3SwapCallback(a0, a1, "");
    }

    function callExecuteOperation(
        address arb,
        address asset,
        uint256 amount,
        uint256 premium,
        address initiator,
        bytes calldata params
    ) external returns (bool) {
        return FlashArb(arb).executeOperation(asset, amount, premium, initiator, params);
    }

    /// @dev The loan's initiator is this attacker, not FlashArb.
    function startLoan(address aave, address arb, address asset, uint256 amount, bytes calldata params) external {
        MockAavePool(aave).flashLoanSimple(arb, asset, amount, params, 0);
    }
}
