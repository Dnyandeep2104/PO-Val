"""F5AI Integration package.

Provides client access to F5's internal OpenAI-compatible AI gateway
(https://f5ai.pd.f5net.com/openai/) for intelligent exception analysis
and automated SOS review summaries.
"""

from .client import F5AIClient

__all__ = ["F5AIClient"]
