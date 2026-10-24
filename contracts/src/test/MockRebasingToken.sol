// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

/// @notice TEST ONLY. Share based token whose balances scale with `index` (1e18 = 1.0).
/// `armRebase(skip, newIndex)` changes the index right after the (skip+1)-th following transfer, i.e. in the
/// middle of a route.
contract MockRebasingToken {
    string public name = "Rebasing";
    string public symbol = "REB";
    uint8 public constant decimals = 18;
    uint256 public index = 1e18;
    uint256 public totalShares;
    mapping(address => uint256) public sharesOf;
    mapping(address => mapping(address => uint256)) public allowance;

    bool public armed;
    uint256 public skip;
    uint256 public newIndex;

    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner, address indexed spender, uint256 value);

    function totalSupply() external view returns (uint256) {
        return (totalShares * index) / 1e18;
    }

    function balanceOf(address a) public view returns (uint256) {
        return (sharesOf[a] * index) / 1e18;
    }

    function _toShares(uint256 amount) internal view returns (uint256) {
        return (amount * 1e18 + index - 1) / index; // round up
    }

    function mint(address to, uint256 amount) external {
        uint256 s = _toShares(amount);
        sharesOf[to] += s;
        totalShares += s;
        emit Transfer(address(0), to, amount);
    }

    function setIndex(uint256 i) external {
        index = i;
    }

    function armRebase(uint256 skipTransfers, uint256 idx) external {
        armed = true;
        skip = skipTransfers;
        newIndex = idx;
    }

    function approve(address s, uint256 v) external returns (bool) {
        allowance[msg.sender][s] = v;
        emit Approval(msg.sender, s, v);
        return true;
    }

    function transfer(address to, uint256 v) external returns (bool) {
        _move(msg.sender, to, v);
        return true;
    }

    function transferFrom(address from, address to, uint256 v) external returns (bool) {
        uint256 a = allowance[from][msg.sender];
        if (a != type(uint256).max) {
            require(a >= v, "allowance");
            allowance[from][msg.sender] = a - v;
        }
        _move(from, to, v);
        return true;
    }

    function _move(address from, address to, uint256 v) internal {
        uint256 s = _toShares(v);
        require(sharesOf[from] >= s, "balance");
        sharesOf[from] -= s;
        sharesOf[to] += s;
        emit Transfer(from, to, v);
        if (armed) {
            if (skip == 0) {
                armed = false;
                index = newIndex;
            } else {
                skip--;
            }
        }
    }
}
