// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";

/// @notice TEST ONLY. ERC-777 style hook: after the (skip+1)-th transfer following `arm`, calls `target` with
/// `data` once. The result of that inner call is recorded (it never reverts the transfer).
contract MockHookToken is ERC20 {
    address public target;
    bytes public data;
    bool public armed;
    uint256 public skip;
    uint256 public fired;
    bool public lastOk;
    bytes public lastRet;

    constructor(string memory n, string memory s) ERC20(n, s) {}

    function mint(address to, uint256 amount) external {
        _mint(to, amount);
    }

    function arm(address t, bytes calldata d, uint256 skipTransfers) external {
        target = t;
        data = d;
        skip = skipTransfers;
        armed = true;
        fired = 0;
        lastOk = false;
        delete lastRet;
    }

    function _update(address from, address to, uint256 value) internal override {
        super._update(from, to, value);
        if (armed && from != address(0) && to != address(0)) {
            if (skip > 0) {
                skip--;
            } else {
                armed = false;
                fired++;
                (lastOk, lastRet) = target.call(data);
            }
        }
    }
}
