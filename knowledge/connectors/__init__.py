"""
Connector exports for lightweight external evidence search.
"""
from knowledge.connectors.base import BaseConnector, RetrievedChunk
from knowledge.connectors.web_search import WebSearchConnector

__all__ = ["BaseConnector", "RetrievedChunk", "WebSearchConnector"]
