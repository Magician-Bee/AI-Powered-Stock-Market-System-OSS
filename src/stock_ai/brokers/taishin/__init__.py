from .official_sdk import TaishinOfficialQuoteAdapter
from .readonly_onboarding import build_taishin_readonly_onboarding_plan


def build_adapter() -> TaishinOfficialQuoteAdapter:
    return TaishinOfficialQuoteAdapter()


__all__ = ["TaishinOfficialQuoteAdapter", "build_adapter", "build_taishin_readonly_onboarding_plan"]
