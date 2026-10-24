// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {FlashArb, Venue} from "../FlashArb.sol";

struct TokenCfg {
    address token;
    uint256 maxLoan; // allowed = true
}

struct PoolCfg {
    address pool;
    uint8 venue;
    uint16 feeBps;
}

/// @notice Constructor-only harness. Used as `eth_call` initcode (no `to`): deploys a fresh FlashArb owned by
/// itself, whitelists tokens and pools, runs the exact executeArb calldata and returns
/// abi.encode(bool success, uint256 profit, uint256 gasUsed, bytes revertData). Never deployed for real.
contract FlashArbSimHarness {
    constructor(
        address aavePool,
        address loanToken,
        TokenCfg[] memory tokens,
        PoolCfg[] memory poolCfgs,
        bytes memory executeArbCalldata
    ) {
        FlashArb arb = new FlashArb(aavePool, address(this));
        bool ok = true;
        bytes memory ret;

        for (uint256 i = 0; i < tokens.length && ok; i++) {
            (ok, ret) = address(arb).call(
                abi.encodeCall(FlashArb.setToken, (tokens[i].token, true, tokens[i].maxLoan))
            );
        }
        for (uint256 i = 0; i < poolCfgs.length && ok; i++) {
            // a venue value above 3 makes the ABI decoder inside FlashArb revert (reported, not bubbled)
            (ok, ret) = address(arb).call(
                abi.encodeWithSelector(
                    FlashArb.setPool.selector,
                    poolCfgs[i].pool,
                    poolCfgs[i].venue,
                    poolCfgs[i].feeBps,
                    true
                )
            );
        }

        uint256 profit;
        uint256 gasUsed;
        if (ok) {
            uint256 before = IERC20(loanToken).balanceOf(address(this));
            uint256 g0 = gasleft();
            (ok, ret) = address(arb).call(executeArbCalldata);
            gasUsed = g0 - gasleft();
            uint256 afterBal = IERC20(loanToken).balanceOf(address(this));
            profit = afterBal >= before ? afterBal - before : 0;
            if (ok) {
                uint256 returned = abi.decode(ret, (uint256));
                if (returned != profit) {
                    ok = false;
                    ret = abi.encodeWithSignature("HarnessProfitMismatch(uint256,uint256)", returned, profit);
                }
            }
        }

        bytes memory out = abi.encode(ok, profit, gasUsed, ok ? bytes("") : ret);
        assembly {
            return(add(out, 32), mload(out))
        }
    }
}
