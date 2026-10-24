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
    bool public skipCallback; // lend nothing and never call executeOperation (a wrong / non-Aave "pool")
    bool public doubleCallback; // call executeOperation twice for one loan
    uint256 public pullShortBy; // pull this much less than amount + premium (leaves an allowance behind)

    function setFakeInitiator(address a) external {
        fakeInitiator = a;
    }

    function setTamperParams(bool b) external {
        tamperParams = b;
    }

    function setSkipCallback(bool b) external {
        skipCallback = b;
    }

    function setDoubleCallback(bool b) external {
        doubleCallback = b;
    }

    function setPullShortBy(uint256 v) external {
        pullShortBy = v;
    }

    function flashLoanSimple(
        address receiver,
        address asset,
        uint256 amount,
        bytes calldata params,
        uint16
    ) external {
        if (skipCallback) return;
        uint256 premium = (amount * 5 + 5000) / 10000;
        IERC20(asset).safeTransfer(receiver, amount);
        address initiator = fakeInitiator != address(0) ? fakeInitiator : msg.sender;
        bytes memory p = tamperParams ? bytes.concat(params, hex"00") : params;
        bool ok = IFlashLoanSimpleReceiverMinimal(receiver).executeOperation(asset, amount, premium, initiator, p);
        require(ok, "MockAave: receiver returned false");
        if (doubleCallback) {
            ok = IFlashLoanSimpleReceiverMinimal(receiver).executeOperation(asset, amount, premium, initiator, p);
            require(ok, "MockAave: receiver returned false");
        }
        IERC20(asset).safeTransferFrom(receiver, address(this), amount + premium - pullShortBy);
    }
}
