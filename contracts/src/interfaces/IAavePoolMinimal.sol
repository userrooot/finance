// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

/// @notice Only the Aave v3 call FlashArb uses (selector 0x42b0b77c).
interface IAavePoolMinimal {
    function flashLoanSimple(
        address receiverAddress,
        address asset,
        uint256 amount,
        bytes calldata params,
        uint16 referralCode
    ) external;
}
