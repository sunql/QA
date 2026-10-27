from app.config import Settings


def test_orchestration_is_disabled_and_bounded_by_default():
    s = Settings(_env_file=None)
    assert s.enableAgentOrchestration is False
    assert s.agentOrchestrationPollSeconds == 2.0
    assert s.agentOrchestrationLeaseSeconds == 60
    assert s.agentOrchestrationMaxIterations == 20
    assert s.agentOrchestrationMaxToolCalls == 30
    assert s.agentOrchestrationMaxCostUsd == 5.0
    assert s.agentOrchestrationMaxTimeoutSeconds == 900
    assert s.agentOrchestrationMaxObservationChars == 50000
    assert s.agentOrchestrationMaxPlannerCorrections == 2
    assert s.agentOrchestrationMaxToolFailures == 2
