// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {FlashArb, Route, Venue} from "../FlashArb.sol";

/// @notice TEST ONLY. Owns a FlashArb and forwards calls from anyone, so a hook token can re-enter through the
/// owner (passes onlyOwner) and be stopped only by nonReentrant.
contract OwnerWrapper {
    FlashArb public immutable arb;

    constructor(address aavePool) {
        arb = new FlashArb(aavePool, address(this));
    }

    function executeArb(Route calldata r) external returns (uint256) {
        return arb.executeArb(r);
    }

    function sweep(address token, uint256 amount) external {
        arb.sweep(token, amount);
    }

    function setToken(address token, bool allowed, uint256 cap) external {
        arb.setToken(token, allowed, cap);
    }

    function setPool(address pool, Venue venue, uint16 feeBps, bool enabled) external {
        arb.setPool(pool, venue, feeBps, enabled);
    }
}
