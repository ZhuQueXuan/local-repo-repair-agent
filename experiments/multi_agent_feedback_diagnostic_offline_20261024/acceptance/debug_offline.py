import sys,traceback
sys.path.insert(0,'scripts')
import test_collaboration_diagnostic as t
from collaboration_live import LiveRunner
for method in ('repair','review'):
    original=getattr(LiveRunner,method)
    def traced(self,context,_original=original):
        try:return _original(self,context)
        except Exception:
            traceback.print_exc()
            raise
    setattr(LiveRunner,method,traced)
case=t.DiagnosticTests('test_actual_full_feedback_preserves_session_versions_and_replay')
case.setUp()
case.test_actual_full_feedback_preserves_session_versions_and_replay()
