"""Local Agent hook: retain current intent and stop persistent repeated reads."""
from minisweagent.agents.default import DefaultAgent
from minisweagent.exceptions import InterruptAgentFlow


class NoProgress(InterruptAgentFlow):
    pass


class StatefulAgent(DefaultAgent):
    def query(self):
        reason=self.env.task_state.cancellation_reason()
        if reason:
            raise InterruptAgentFlow({'role':'exit','content':'ExperimentStopped',
                'extra':{'exit_status':'ExperimentStopped','submission':'','reason':reason}})
        reason = self.env.task_state.stagnation_reason()
        if reason:
            raise NoProgress({'role':'exit','content':'NoProgress',
                'extra':{'exit_status':'NoProgress','submission':'','reason':reason}})
        return super().query()

    def execute_actions(self, message):
        self.env.task_state.record_intent(message.get("content"))
        return super().execute_actions(message)

