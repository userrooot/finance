// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";

interface IUniV3CbMock {
    function uniswapV3SwapCallback(int256 a0, int256 a1, bytes calldata d) external;
}

interface IPancakeV3CbMock {
    function pancakeV3SwapCallback(int256 a0, int256 a1, bytes calldata d) external;
}

/// @notice TEST ONLY. Fixed price v3 style pool: zeroForOne gives `in * num / den` of token1,
/// the other direction gives `in * den / num` of token0.
contract MockV3Pool {
    using SafeERC20 for IERC20;

    enum Mode {
        NORMAL,
        DOUBLE_CALLBACK, // calls the callback twice
        OVERASK, // asks for amountIn + 1
        PARTIAL, // fills half
        SHORT_PAY, // reports `out` but sends out - 1
        NEG_OWED, // reports a negative amount on the input side
        NO_CALLBACK, // never calls back
        UNDERASK // asks for amountIn - 1 in the callback but reports the full amountIn as paid
    }

    address public token0;
    address public token1;
    bool public pancake;
    uint256 public num = 1;
    uint256 public den = 1;
    Mode public mode;

    constructor(address t0, address t1, bool pancake_) {
        token0 = t0;
        token1 = t1;
        pancake = pancake_;
    }

    function setRate(uint256 n, uint256 d) external {
        num = n;
        den = d;
    }

    function setMode(Mode m) external {
        mode = m;
    }

    function _callback(int256 a0, int256 a1) internal {
        if (mode == Mode.UNDERASK) {
            // ask for one less than swap() will report as paid
            if (a0 > 0) a0 -= 1;
            else a1 -= 1;
        }
        if (pancake) IPancakeV3CbMock(msg.sender).pancakeV3SwapCallback(a0, a1, "");
        else IUniV3CbMock(msg.sender).uniswapV3SwapCallback(a0, a1, "");
    }

    function swap(
        address recipient,
        bool zeroForOne,
        int256 amountSpecified,
        uint160,
        bytes calldata
    ) external returns (int256 a0, int256 a1) {
        require(amountSpecified > 0, "exact input only");
        uint256 amountIn = uint256(amountSpecified);
        uint256 owed = amountIn;
        uint256 out = zeroForOne ? (amountIn * num) / den : (amountIn * den) / num;
        uint256 sent = out;
        Mode m = mode;
        if (m == Mode.PARTIAL) {
            owed = amountIn / 2;
            out = out / 2;
            sent = out;
        } else if (m == Mode.OVERASK) {
            owed = amountIn + 1;
        } else if (m == Mode.SHORT_PAY) {
            sent = out - 1;
        }
        (IERC20 tIn, IERC20 tOut) = zeroForOne
            ? (IERC20(token0), IERC20(token1))
            : (IERC20(token1), IERC20(token0));

        tOut.safeTransfer(recipient, sent);

        int256 inDelta = m == Mode.NEG_OWED ? -int256(owed) : int256(owed);
        (a0, a1) = zeroForOne ? (inDelta, -int256(out)) : (-int256(out), inDelta);

        if (m == Mode.NO_CALLBACK) return (a0, a1);

        uint256 balBefore = tIn.balanceOf(address(this));
        _callback(a0, a1);
        if (m == Mode.DOUBLE_CALLBACK) _callback(a0, a1);
        if (m == Mode.UNDERASK) owed -= 1;
        if (m != Mode.NEG_OWED) require(tIn.balanceOf(address(this)) >= balBefore + owed, "MockV3: not paid");
    }
}
