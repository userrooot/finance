// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Route} from "../FlashArb.sol";

/// @notice TEST ONLY. Golden-vector helper.
contract RouteHashProbe {
    function hashParams(uint256 balBefore, Route calldata r) external pure returns (bytes32) {
        return keccak256(abi.encode(balBefore, r));
    }
}
