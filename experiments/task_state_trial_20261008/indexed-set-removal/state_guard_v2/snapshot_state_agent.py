"""Local Agent hook: retain current intent and stop persistent repeated reads."""
from minisweagent.agents.default import DefaultAgent
from minisweagent.exceptions import InterruptAgentFlow


class NoProgress(InterruptAgentFlow):
    pass


class StatefulAgent(DefaultAgent):
    def execute_actions(self, message):
        self.env.task_state.record_intent(message.get("content"))
        return super().execute_actions(message)

