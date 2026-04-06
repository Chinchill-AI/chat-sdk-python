"""Integration tests for chat-sdk.

These tests exercise end-to-end flows through the Chat orchestrator using
MockAdapter and MemoryStateAdapter, validating that handler registration,
message routing, subscriptions, concurrency, deduplication, reactions,
actions, and slash commands all work together correctly.
"""
