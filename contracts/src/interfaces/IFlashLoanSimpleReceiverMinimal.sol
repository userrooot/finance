// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

/// @notice Aave v3 simple flash-loan receiver callback (own minimal copy; Aave's base is not imported).
interface IFlashLoanSimpleReceiverMinimal {
    function executeOperation(
        address asset,
        uint256 amount,
        uint256 premium,
        address initiator,
        bytes calldata params
    ) external returns (bool);
}
