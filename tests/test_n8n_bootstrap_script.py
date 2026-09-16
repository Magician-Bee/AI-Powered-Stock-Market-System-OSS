from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_n8n_owner_bootstrap_recovers_only_a_stale_local_gateway_key() -> None:
    source = (ROOT / "scripts" / "setup-n8n-local-owner.sh").read_text(encoding="utf-8")

    assert 'existing_owner_email="$(read_gateway_value N8N_LOCAL_OWNER_EMAIL || true)"' in source
    assert 'existing_owner_password="$(read_gateway_value N8N_LOCAL_OWNER_PASSWORD || true)"' in source
    assert "emailOrLdapLoginId:$email" in source
    assert '"$N8N_URL/rest/login"' in source
    assert "authenticated the existing local owner to recover a scoped replacement" in source
    assert "no valid local API key or local owner recovery credentials" in source
    assert 'callback_secret="$existing_callback_secret"' in source
    assert 'N8N_AUTOMATION_GATEWAY_GENERATION=%s' in source
    assert 'ensure_gateway_generation' in source
    assert "N8N_AUTOMATION_GATEWAY_TOKEN=%s" in source
    assert 'execution:(list|read|delete)' in source
    assert 'gateway_key_is_ready "$existing_token"' in source
