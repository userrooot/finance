// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {IFlashLoanSimpleReceiverMinimal} from "../interfaces/IFlashLoanSimpleReceiverMinimal.sol";

/// @notice TEST ONLY. Mimics Aave v3 flashLoanSimple (premium 5 bps, rounds half up).
contract MockAavePool {
    using SafeERC20 for IERC20;

    address public fakeInitiator; // if non-zero, passed instead of msg.sender
    bool public tamperParams; // append a byte to params

    function setFakeInitiator(address a) external {
        fakeInitiator = a;
    }

    function setTamperParams(bool b) external {
        tamperParams = b;
    }

    function flashLoanSimple(
        address receiver,
        address asset,
        uint256 amount,
        bytes calldata params,
        uint16
    ) external {
        uint256 premium = (amount * 5 + 5000) / 10000;
        IERC20(asset).safeTransfer(receiver, amount);
        address initiator = fakeInitiator != address(0) ? fakeInitiator : msg.sender;
        bytes memory p = tamperParams ? bytes.concat(params, hex"00") : params;
        bool ok = IFlashLoanSimpleReceiverMinimal(receiver).executeOperation(asset, amount, premium, initiator, p);
        require(ok, "MockAave: receiver returned false");
        IERC20(asset).safeTransferFrom(receiver, address(this), amount + premium);
    }
}
