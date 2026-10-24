// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {FlashArb, Hop} from "../FlashArb.sol";

/// @notice TEST ONLY. Single-hop entry point for per-venue fork checks, and getters for the in-flight slots (I3).
contract FlashArbExposed is FlashArb {
    constructor(address aavePool, address initialOwner) FlashArb(aavePool, initialOwner) {}

    /// @dev The caller must have funded this contract with `amountIn` of h.tokenIn. Output stays here.
    function swapOnce(Hop calldata h, uint256 amountIn) external onlyOwner nonReentrant returns (uint256) {
        _checkHop(0, h);
        return _swap(0, h, amountIn);
    }

    function routeHash() external view returns (bytes32) {
        return _routeHash;
    }

    function pendingPool() external view returns (address) {
        return _pending.pool;
    }

    function pendingZeroForOne() external view returns (bool) {
        return _pending.zeroForOne;
    }

    function pendingMaxPay() external view returns (uint256) {
        return _pendingMaxPay;
    }

    function lastPremium() external view returns (uint256) {
        return _lastPremium;
    }
}
