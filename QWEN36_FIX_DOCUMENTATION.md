# qwen3.6 Infinite Loop Vulnerability Fix

## Date
2025-04-25

## Problem Identified
qwen3.6 models were experiencing infinite loops due to **circular fallback configuration** in LiteLLM proxy settings.

## Root Causes

### 1. Circular Fallback Chains (PRIMARY VULNERABILITY)
The fallback configuration created circular dependencies:
- `qwen3.6-35b-176` → `glm-4.7` → `qwen3.6-35b-176` → ∞
- `qwen3.6-35b-176` → `glm-4.6` → `qwen3.6-35b-176` → ∞
- `saia/qwen3.5-35b-a3b` → `qwen3.6-35b-176` → `glm-4.7` → `saia/qwen3.5-35b-a3b` → ∞

### 2. Known LiteLLM Bugs
- **Issue #26015** (OPEN - April 2026): Streaming + 429 mid-stream + no fallbacks = 100% CPU hang
- **PR #7751** (Merged Jan 14, 2025): Fallback handler calling wrong function (pre-Jan 2025 versions）

## Fixed Configuration Files

### 1. litellm_proxy_config.yaml
Changed from circular chains to linear fallbacks:

**BEFORE (CIRCULAR):**
```yaml
fallbacks:
  - {"qwen3.6-35b-176": ["saia/qwen3.5-35b-a3b", "glm-4.7"]}
  - {"glm-4.7": ["saia/glm-4.7", "glm-4.6", "qwen3.6-35b-176"]}  # ⚠️ CIRCULAR!
```

**AFTER (LINEAR):**
```yaml
fallbacks:
  - {"qwen3.6-35b-176": ["saia/qwen3.5-35b-a3b"]}
  - {"glm-4.7": ["saia/glm-4.7", "glm-4.6"]}
```

### 2. opencode_proxy_config.yaml
Simplified to single-tier fallbacks:

**BEFORE:**
```yaml
fallbacks:
  - qwen3.6-35b: [gemma-4-26b-nvfp4, gemma-4-26b]
  - gemma-4-26b-nvfp4: [gemma-4-26b]
```

**AFTER:**
```yaml
fallbacks:
  - qwen3.6-35b: [gemma-4-26b]
  - qwen3.6-35b/ai2: [gemma-4-26b]
```

## Verification Steps

### 1. Check LiteLLM Version
```bash
poetry show litellm | grep version
```
**Required:** Version must be post-Jan 14, 2025 (for PR #7751 fix)

### 2. Test Fallback Chains
```python
import litellm
# Test that fallbacks complete without infinite loops
response = litellm.completion(
    model="qwen3.6-35b",
    messages=[{"role": "user", "content": "test"}],
    fallbacks=["gemma-4-26b"]
)
assert response is not None
```

### 3. Monitor Logs for Deep Fallbacks
```bash
# Check fallback depth stays below DEFAULT_MAX_RECURSE_DEPTH
grep "fallback_depth" /litellm/logs/*.log | tail -20
```

## Protection Mechanisms (Still Active)

1. **DEFAULT_MAX_RECURSE_DEPTH:**
   - Location: `litellm/proxy/auth/auth_checks.py:2545`
   - Protects against circular alias lookups in auth checks

2. **max_fallbacks Setting:**
   - Current config: Not explicitly set (uses default: 5)
   - Limits total fallback attempts per request

## Recommendations

1. **Never create circular fallback chains** - always use acyclic graphs
2. **Monitor for 429 errors** during streaming (triggers Issue #26015)
3. **Update to latest LiteLLM** after PR #22375 fix for mid-stream 429 handling
4. **Add alerting** for fallback_depth > 3 (indicates problems)

## References

- GitHub Issue #7091: completion call with fallbacks stuck in infinite loop
- GitHub Issue #26015: No fallbacks + 429 mid-stream causes 100% CPU hang (OPEN)
- GitHub PR #7751: Fix fallbacks stuck in infinite loop (merged Jan 14, 2025)
- GitHub Issue #23546: Infinite retry loop when reasoning_effort="none" (OPEN)
- QwenLM/Qwen3.6 Issue #88: Repetition/Looping issue in Qwen3.5-35B-A3B model