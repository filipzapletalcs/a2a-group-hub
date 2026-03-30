"""Graphiti knowledge graph search client for hub context enrichment."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger("a2a-hub.graphiti")


class GraphitiSearchClient:
    """Thin wrapper around graphiti-core search for hub context enrichment.

    Provides timeout-protected, fault-tolerant search that returns a formatted
    ``[Knowledge Graph Facts]`` section or empty string on any failure.
    """

    def __init__(self) -> None:
        self._client = None
        self._initialized = False
        self._search_timeout = float(os.environ.get("GRAPHITI_SEARCH_TIMEOUT", "2.0"))

    async def initialize(self) -> bool:
        """Lazy-init Graphiti client. Returns True on success, False otherwise."""
        try:
            from graphiti_core import Graphiti
            from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient
            from graphiti_core.llm_client.config import LLMConfig
            from graphiti_core.embedder.voyage import VoyageAIEmbedder, VoyageAIEmbedderConfig
            from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient
        except ImportError:
            logger.warning(
                "graphiti-core not installed -- Graphiti search disabled",
                extra={"event": "graphiti_search_import_failed"},
            )
            return False

        api_key = os.environ.get("GRAPHITI_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
        model = os.environ.get("GRAPHITI_LLM_MODEL", "gpt-4o-mini")
        neo4j_uri = os.environ.get("NEO4J_URI", "bolt://neo4j:7687")
        neo4j_user = os.environ.get("NEO4J_USER", "neo4j")
        neo4j_password = os.environ.get("NEO4J_PASSWORD", "")
        voyage_key = os.environ.get("VOYAGE_API_KEY", "")

        if not neo4j_password:
            logger.warning(
                "NEO4J_PASSWORD not set -- Graphiti search disabled",
                extra={"event": "graphiti_search_missing_env", "var": "NEO4J_PASSWORD"},
            )
            return False

        if not api_key:
            logger.warning(
                "GRAPHITI_LLM_API_KEY / OPENAI_API_KEY not set -- Graphiti search disabled",
                extra={"event": "graphiti_search_missing_env", "var": "OPENAI_API_KEY"},
            )
            return False

        try:
            llm_config = LLMConfig(
                api_key=api_key,
                model=model,
                small_model=model,
            )

            embedder_config = VoyageAIEmbedderConfig(
                api_key=voyage_key,
                embedding_model="voyage-3",
            )

            graphiti = Graphiti(
                uri=neo4j_uri,
                user=neo4j_user,
                password=neo4j_password,
                llm_client=OpenAIGenericClient(config=llm_config),
                embedder=VoyageAIEmbedder(config=embedder_config),
                cross_encoder=OpenAIRerankerClient(config=llm_config),
                store_raw_episode_content=True,
            )

            self._client = graphiti
            self._initialized = True

            logger.info(
                "Graphiti search client initialized",
                extra={
                    "event": "graphiti_search_client_initialized",
                    "neo4j_uri": neo4j_uri,
                    "model": model,
                    "timeout": self._search_timeout,
                },
            )
            return True

        except Exception as exc:
            logger.warning(
                "Graphiti search client init failed: %s",
                exc,
                extra={"event": "graphiti_search_init_failed", "error": str(exc)},
            )
            return False

    async def search_facts(self, query: str, num_results: int = 5) -> str:
        """Search Graphiti for relevant facts.

        Returns a formatted ``[Knowledge Graph Facts]`` section string, or
        empty string if unavailable / no results.
        """
        if not self._initialized or self._client is None:
            return ""

        try:
            results = await asyncio.wait_for(
                self._client.search(query=query, num_results=num_results),
                timeout=self._search_timeout,
            )

            if not results or not getattr(results, "edges", None):
                return ""

            edges = results.edges[:num_results]
            now = datetime.now(timezone.utc)
            lines: list[str] = []
            count_expired = 0

            for edge in edges:
                fact = getattr(edge, "fact", str(edge))
                validity_suffix = ""

                if hasattr(edge, "invalid_at") and edge.invalid_at:
                    # Compare with current UTC time
                    invalid_at = edge.invalid_at
                    if hasattr(invalid_at, "tzinfo") and invalid_at < now:
                        validity_suffix = " [EXPIRED]"
                        count_expired += 1
                    elif hasattr(edge, "valid_at") and edge.valid_at:
                        valid_str = (
                            edge.valid_at.strftime("%Y-%m-%d")
                            if hasattr(edge.valid_at, "strftime")
                            else str(edge.valid_at)[:10]
                        )
                        invalid_str = (
                            edge.invalid_at.strftime("%Y-%m-%d")
                            if hasattr(edge.invalid_at, "strftime")
                            else str(edge.invalid_at)[:10]
                        )
                        validity_suffix = f" [valid: {valid_str} -> {invalid_str}]"

                lines.append(f"- {fact}{validity_suffix}")

            if not lines:
                return ""

            logger.info(
                "Graphiti search complete",
                extra={
                    "event": "graphiti_search_complete",
                    "query_length": len(query),
                    "results": len(edges),
                    "expired": count_expired,
                },
            )

            return "[Knowledge Graph Facts]\n" + "\n".join(lines)

        except asyncio.TimeoutError:
            logger.warning(
                "Graphiti search timed out after %.1fs",
                self._search_timeout,
                extra={
                    "event": "graphiti_search_timeout",
                    "timeout": self._search_timeout,
                },
            )
            return ""

        except Exception as exc:
            logger.debug(
                "Graphiti search unavailable: %s",
                exc,
                extra={"event": "graphiti_search_error", "error": str(exc)},
            )
            return ""

    async def close(self) -> None:
        """Close the underlying Graphiti client connection."""
        if self._client:
            try:
                await self._client.close()
                logger.info(
                    "Graphiti search client closed",
                    extra={"event": "graphiti_search_client_closed"},
                )
            except Exception as exc:
                logger.debug("Error closing Graphiti client: %s", exc)
