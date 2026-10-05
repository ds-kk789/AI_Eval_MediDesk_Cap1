"""One JSON line per request: who asked, what was retrieved, allowed or denied.

Port from: new

TODO:
- log_request(role, endpoint, tools_called, chunk_ids, decision, latency_ms)
- Append to config.AUDIT_LOG; never store question or answer text (BR-13)
"""
