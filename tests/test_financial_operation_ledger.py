from __future__ import annotations

import pytest


def _new_op(storage, key="external-tx-1"):
    return storage.create_financial_operation(
        chain="solana", token_address="So11111111111111111111111111111111111111112",
        side="BUY", size_native=0.1, size_usd=10, idempotency_key=key,
    )


def test_idempotency_key_prevents_duplicate_operation(storage):
    first = _new_op(storage)
    second = storage.create_financial_operation(
        chain="solana", token_address="So11111111111111111111111111111111111111112",
        side="BUY", size_native=0.1, size_usd=10, idempotency_key="external-tx-1",
    )
    assert first["created"] is True
    assert second["created"] is False
    assert second["operation_id"] == first["operation_id"]
    with pytest.raises(ValueError, match="idempotency key collision"):
        storage.create_financial_operation(chain="solana", token_address="DifferentMint", side="BUY",
                                           size_native=9, size_usd=900, idempotency_key="external-tx-1")
    assert len(storage.list_financial_operations()) == 1


def test_pnl_is_refused_until_confirmed(storage):
    op = _new_op(storage)
    with pytest.raises(PermissionError, match="requires CONFIRMED"):
        storage.apply_verified_operation_pnl(
            op["operation_id"], gross_pnl_usd=2, fees_usd=0.1,
            slippage_usd=0.1, gas_usd=0.1,
        )
    assert storage.verified_operation_pnl_total() == 0


def test_confirmed_pnl_is_net_and_idempotent(storage):
    op = _new_op(storage)
    storage.transition_financial_operation(op["operation_id"], "CONFIRMED", tx_hash="test-signature",
                                           fees_usd=0.1, slippage_usd=0.05, gas_usd=0.05,
                                           verification_evidence={"provider": "test-adapter", "status": "CONFIRMED",
                                                                  "tx_hash": "test-signature", "confirmations": 1})
    first = storage.apply_verified_operation_pnl(
        op["operation_id"], gross_pnl_usd=2.0, fees_usd=0.1,
        slippage_usd=0.05, gas_usd=0.05,
    )
    second = storage.apply_verified_operation_pnl(
        op["operation_id"], gross_pnl_usd=2.0, fees_usd=0.1,
        slippage_usd=0.05, gas_usd=0.05,
    )
    assert first["applied"] is True
    assert first["net_pnl_usd"] == pytest.approx(1.8)
    assert second["applied"] is False
    with pytest.raises(ValueError, match="conflicting PnL payload"):
        storage.apply_verified_operation_pnl(
            op["operation_id"], gross_pnl_usd=9.0, fees_usd=0.1,
            slippage_usd=0.05, gas_usd=0.05,
        )
    assert storage.verified_operation_pnl_total() == pytest.approx(1.8)
    assert len(storage.verified_operation_pnl_events()) == 1


def test_confirmed_requires_transaction_hash(storage):
    op = _new_op(storage)
    with pytest.raises(ValueError, match="tx_hash"):
        storage.transition_financial_operation(op["operation_id"], "CONFIRMED")
    with pytest.raises(PermissionError, match="provider evidence"):
        storage.transition_financial_operation(op["operation_id"], "CONFIRMED", tx_hash="sig-only")


def test_terminal_state_cannot_be_reopened(storage):
    op = _new_op(storage)
    storage.transition_financial_operation(op["operation_id"], "FAILED", reject_reason="provider reject")
    with pytest.raises(ValueError, match="illegal operation transition"):
        storage.transition_financial_operation(op["operation_id"], "CONFIRMED", tx_hash="bad")


def test_invalid_numbers_are_rejected(storage):
    with pytest.raises(ValueError, match="size_usd"):
        storage.create_financial_operation(chain="solana", token_address="mint", side="BUY",
                                           size_native=1, size_usd=float("nan"))


def test_pnl_costs_must_match_confirmed_operation(storage):
    op = _new_op(storage)
    storage.transition_financial_operation(op["operation_id"], "CONFIRMED", tx_hash="cost-check-signature",
                                           fees_usd=0.1, slippage_usd=0.05, gas_usd=0.05,
                                           verification_evidence={"provider": "test-adapter", "status": "CONFIRMED",
                                                                  "tx_hash": "cost-check-signature", "confirmations": 1})
    with pytest.raises(ValueError, match="costs must match"):
        storage.apply_verified_operation_pnl(op["operation_id"], gross_pnl_usd=2,
                                             fees_usd=0, slippage_usd=0, gas_usd=0)
    assert storage.verified_operation_pnl_total() == 0
