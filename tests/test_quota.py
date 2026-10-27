import time
import pytest
from native_swarm.quota import normalize


def test_live_quota_cutoff_and_expired_windows():
 q=normalize({'rateLimits':{'primary':{'usedPercent':50,'resetsAt':time.time()+60},'secondary':{'usedPercent':99,'resetsAt':time.time()-60}}})
 assert q['reserve_reached'] and len(q['active_windows'])==1 and q['model_calls']==0


def test_missing_quota_never_becomes_zero_usage():
 with pytest.raises(RuntimeError):normalize({'rateLimits':{}})
