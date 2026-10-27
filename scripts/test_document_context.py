"""Regression for the actual document receipt/context failure; no live API."""
from copy import deepcopy
import json
import os
os.environ['LITELLM_LOCAL_MODEL_COST_MAP']='True'
import unittest
from unittest.mock import patch
import uuid
from types import SimpleNamespace

from collaboration_protocol import canonical_bytes, sha256
from context_budget import message_bytes
from document_agent import DocumentBroker, freeze_documents, DocumentHandoffTooLarge, DocumentReadRangeError, document_rejection
from document_model import ProtectedDocumentModel
from collaboration_runtime import run_document_consultation, run_review
from collaboration_protocol import ProtocolError
from review_model import ReviewModel
from minisweagent.exceptions import FormatError, LimitsExceeded
from review_tools import ReviewCancelled, ReviewEvidenceBroker
from test_collaboration_protocol import StageAFixture
import test_document_agent as document_fixture
import test_collaboration_runtime as runtime_fixture


class DocumentContextTests(unittest.TestCase):
    def response_batch(self, calls):
        response=runtime_fixture.response(calls[0][0],calls[0][1])
        template=response.choices[0].message.tool_calls[0]
        response.choices[0].message.tool_calls=[template.model_copy(deep=True) for _ in calls]
        for i,(tool,args) in enumerate(calls):
            response.choices[0].message.tool_calls[i].id='batch-call-'+str(i)
            response.choices[0].message.tool_calls[i].function.name=tool
            response.choices[0].message.tool_calls[i].function.arguments=json.dumps(args)
        return response

    def fixture(self, raw):
        # Reuse the file fixture without importing its TestCase into discovery.
        f=document_fixture.DocumentTests();f.setUp()
        path=f.f.baseline/'docs/rules.md';path.write_bytes(raw)
        manifest=deepcopy(f.f.manifest);manifest['source_files_sha256']['docs/rules.md']=sha256(raw)
        snapshot=freeze_documents(manifest,f.f.baseline,f.f.issue,f.f.root/'context-docs',f.identity,'Locate documented split boundary requirements.')
        return f,DocumentBroker(snapshot,f.identity)

    def range_fixture(self):
        f,_=self.fixture(b'short\n'*269)
        lines=['other\n']*900;lines[822]='target-evidence\n'
        (f.f.baseline/'docs/long.md').write_text(''.join(lines),encoding='utf-8')
        manifest=deepcopy(f.f.manifest)
        for name in ('docs/rules.md','docs/long.md'):
            manifest['source_files_sha256'][name]=sha256((f.f.baseline/name).read_bytes())
        snapshot=freeze_documents(manifest,f.f.baseline,f.f.issue,f.f.root/'range-docs',f.identity,'Locate target-evidence.')
        return f,DocumentBroker(snapshot,f.identity)

    def test_wrong_document_range_can_be_corrected_in_the_same_consultation(self):
        f,broker=self.range_fixture();g=broker.snapshot.grant
        wrong=next(d for d in g.documents if d.line_count==269)
        calls=[]
        def query(messages):
            calls.append(deepcopy(messages))
            if len(calls)==1:
                actions=[('search_text',{'snapshot_id':g.snapshot_id,'query':'target-evidence'}),
                    ('read_lines',{'snapshot_id':g.snapshot_id,'document_id':wrong.document_id,'start':815,'end':835})]
            elif len(calls)==2:
                rejection=json.loads(messages[-1]['content'])
                self.assertEqual(rejection['selected_document']['line_count'],269)
                self.assertEqual(rejection['selected_document']['path'],'docs/rules.md')
                self.assertEqual(rejection['max_lines_per_read'],80)
                self.assertLessEqual(len(canonical_bytes(rejection)),4096)
                match=rejection['previously_issued_matches'][0]
                self.assertEqual(match['path'],'docs/long.md')
                self.assertIn(match['evidence_id'],broker.evidence)
                self.assertEqual(len(broker.evidence),1)  # Rejection issued no citation.
                actions=[('read_lines',{'snapshot_id':rejection['snapshot_id'],'document_id':match['document_id'],
                    'start':match['start_line'],'end':match['end_line']})]
            else:
                actions=[('submit_consultation',{'advice':document_fixture.advice_for(broker).model_dump(mode='json')})]
            message={'role':'assistant','content':'','extra':{'actions':[]},'tool_calls':[]}
            for i,(tool,args) in enumerate(actions):
                identity=f'range-{len(calls)}-{i}'
                message['tool_calls'].append({'id':identity,'type':'function','function':{'name':tool,'arguments':json.dumps(args)}})
                message['extra']['actions'].append({'tool':tool,'arguments':args,'tool_call_id':identity})
            return message
        model=SimpleNamespace(query=query,usage={},contains_credentials=lambda _:False)
        model.format_observation_messages=lambda m,outputs:ReviewModel.format_observation_messages(model,m,outputs)
        result=run_document_consultation(model,broker,SimpleNamespace(check=lambda:None,identity=f.identity),
            f.f.root/'corrected-range.json','One question')
        self.assertEqual(result.status,'answered');self.assertEqual(len(calls),3)
        self.assertEqual(sum(e['kind']=='tool_rejected' for e in broker.events),1)

    def test_range_feedback_without_issued_hits_does_not_expose_other_grants(self):
        f,broker=self.range_fixture();g=broker.snapshot.grant;doc=g.documents[0]
        args={'snapshot_id':g.snapshot_id,'document_id':doc.document_id,'start':999,'end':1000}
        with self.assertRaises(ProtocolError) as caught:broker.dispatch(f.identity,'read_lines',args)
        feedback=document_rejection(caught.exception)
        self.assertEqual(feedback['previously_issued_matches'],[])
        self.assertFalse(broker.evidence)
        for change in ({'snapshot_id':'unknown-snapshot'},{'document_id':'unknown-document'}):
            with self.assertRaises(ProtocolError) as caught:broker.dispatch(f.identity,'read_lines',args|change)
            self.assertNotIn('selected_document',document_rejection(caught.exception))
            self.assertNotIn('previously_issued_matches',document_rejection(caught.exception))
        target=SimpleNamespace(document_id='d'*95,path='🙂'*240,line_count=269)
        matches=[{'document_id':'m'*95,'path':'🙂'*240,'line_count':900,'evidence_id':'e'*95,'start_line':823,'end_line':823}]*3
        bounded=document_rejection(DocumentReadRangeError('s'*95,target,matches))
        self.assertLessEqual(len(canonical_bytes({'status':'rejected',**bounded})),4096)
        self.assertEqual(bounded['selected_document']['path'],target.path)

    def test_repeated_invalid_ranges_keep_the_existing_three_error_stop(self):
        f,broker=self.range_fixture();g=broker.snapshot.grant;count=0
        args={'snapshot_id':g.snapshot_id,'document_id':g.documents[0].document_id,'start':999,'end':1000}
        def query(messages):
            nonlocal count
            count+=1;identity=f'bad-range-{count}'
            return {'role':'assistant','content':'','tool_calls':[{'id':identity,'type':'function',
                'function':{'name':'read_lines','arguments':json.dumps(args)}}],
                'extra':{'actions':[{'tool':'read_lines','arguments':args,'tool_call_id':identity}]}}
        model=SimpleNamespace(query=query,usage={},contains_credentials=lambda _:False)
        model.format_observation_messages=lambda m,outputs:ReviewModel.format_observation_messages(model,m,outputs)
        with self.assertRaisesRegex(ProtocolError,'document_repeated_format_errors'):
            run_document_consultation(model,broker,SimpleNamespace(check=lambda:None,identity=f.identity),f.f.root/'bad-range.json','One question')
        self.assertEqual(count,3);self.assertFalse(broker.evidence);self.assertIsNone(broker.advice)

    def test_search_bounds_complete_citation_envelopes_and_revokes_unreturned_ids(self):
        f,broker=self.fixture((('split_after ' + 'x'*60+'\n')*40).encode())
        result=broker.dispatch(f.identity,'search_text',{'snapshot_id':broker.snapshot.grant.snapshot_id,'query':'split'})
        self.assertLessEqual(len(canonical_bytes(result)),4096)
        self.assertTrue(result['truncated'])
        self.assertLess(len(result['hits']),20)
        self.assertEqual(set(broker.evidence),{h['evidence']['evidence_id'] for h in result['hits']})

    def test_read_accounts_for_json_escaping_and_returns_real_continuation(self):
        f,broker=self.fixture(('"\\'*600+'\n'+('more text '*10+'\n')*30).encode())
        doc=broker.snapshot.grant.documents[0]
        result=broker.dispatch(f.identity,'read_lines',{'snapshot_id':broker.snapshot.grant.snapshot_id,'document_id':doc.document_id,'start':1,'end':31})
        self.assertLessEqual(len(canonical_bytes(result)),4096)
        self.assertTrue(result['truncated'])
        self.assertEqual(result['next_line'],result['evidence']['end_line']+1)
        self.assertEqual(sha256(result['content'].encode()),result['evidence']['span_sha256'])

    def test_long_actual_receipt_history_projects_complete_pairs_without_mutation(self):
        f,broker=self.fixture(('split '+ 'x'*160+'\n') .encode()*40)
        doc=broker.snapshot.grant.documents[0]
        messages=[{'role':'system','content':'Read-only document task.'},{'role':'user','content':'ORIGINAL_QUESTION_AND_REGISTERED_IDS'}]
        for i in range(12):
            args={'snapshot_id':broker.snapshot.grant.snapshot_id,'document_id':doc.document_id,'start':i+1,'end':i+15}
            receipt=broker.dispatch(f.identity,'read_lines',args)
            messages.extend([{'role':'assistant','content':'Unverified interpretation '+str(i),'tool_calls':[{'id':'call-'+str(i),'type':'function',
                'function':{'name':'read_lines','arguments':json.dumps(args)}}]},
                {'role':'tool','tool_call_id':'call-'+str(i),'content':canonical_bytes(receipt).decode()}])
        before=canonical_bytes(messages)
        fixture=runtime_fixture.RuntimeTests();fixture.setUp()
        model=fixture.model(cls=ProtectedDocumentModel)
        prepared=model._prepare_messages_for_api(messages)
        self.assertGreater(message_bytes(messages),16384)
        self.assertLessEqual(message_bytes(prepared),12288)
        self.assertEqual(canonical_bytes(messages),before)
        self.assertEqual(prepared[:2],messages[:2])
        self.assertEqual(prepared[-1]['tool_call_id'],messages[-1]['tool_call_id'])
        for i,m in enumerate(prepared):
            if m['role']=='tool':self.assertEqual(prepared[i-1]['tool_calls'][0]['id'],m['tool_call_id'])
        self.assertTrue(model.document_projection_events)

    def test_optional_indexes_do_not_overflow_a_complete_two_read_group(self):
        f,broker=self.fixture((('source '+'x'*110+'\n')*100).encode())
        doc=broker.snapshot.grant.documents[0]
        messages=[{'role':'system','content':'S'*720},{'role':'user','content':'ORIGINAL_TASK '+'Q'*4540}]
        for i in range(6):
            assistant={'role':'assistant','content':'','tool_calls':[]};outputs=[]
            for j,start in enumerate((i+1,i+45)):
                args={'snapshot_id':broker.snapshot.grant.snapshot_id,'document_id':doc.document_id,'start':start,'end':start+30}
                receipt=broker.dispatch(f.identity,'read_lines',args);identity=f'bounded-group-{i}-{j}'
                assistant['tool_calls'].append({'id':identity,'type':'function','function':{'name':'read_lines','arguments':json.dumps(args)}})
                outputs.append({'role':'tool','tool_call_id':identity,'content':canonical_bytes(receipt).decode()})
            messages.extend([assistant,*outputs])
        fixture=runtime_fixture.RuntimeTests();fixture.setUp();model=fixture.model(cls=ProtectedDocumentModel)
        card={'role':'user','content':'CURRENT_CARD '+'C'*990};model._task_card_receipt={'message':card}
        before=canonical_bytes(messages)
        projected=model._prepare_messages_for_api(messages)
        self.assertLessEqual(message_bytes(projected),16384)
        self.assertEqual(projected[:2],messages[:2]);self.assertEqual(projected[-3:-1],messages[-2:])
        self.assertEqual(sum(m==card for m in projected),1)
        self.assertEqual(canonical_bytes(messages),before)
        latest=projected[-4]['tool_calls']
        self.assertEqual([m['tool_call_id'] for m in projected[-3:-1]],[c['id'] for c in latest])

    def test_rotating_unchanged_queries_stop_without_a_global_request_cap(self):
        f,broker=self.fixture(('split behavior\n'*12).encode())
        doc=broker.snapshot.grant.documents[0]
        requests=[('read_lines',{'snapshot_id':broker.snapshot.grant.snapshot_id,'document_id':doc.document_id,'start':1,'end':1}),
            ('search_text',{'snapshot_id':broker.snapshot.grant.snapshot_id,'query':'split'})]
        observed=[]
        def query(messages):
            tool,args=requests[len(observed)%2];observed.append(deepcopy(messages))
            call_id='cycle-'+str(len(observed))
            return {'role':'assistant','content':'','tool_calls':[{'id':call_id,'type':'function','function':{'name':tool,'arguments':json.dumps(args)}}],
                'extra':{'actions':[{'tool':tool,'arguments':args,'tool_call_id':call_id}]}}
        model=SimpleNamespace(query=query,usage={},contains_credentials=lambda text:False)
        model.format_observation_messages=lambda message,outputs:ReviewModel.format_observation_messages(model,message,outputs)
        context=SimpleNamespace(check=lambda:None,identity=f.identity)
        with self.assertRaisesRegex(ProtocolError,'document_repeated_evidence_limit'):
            run_document_consultation(model,broker,context,f.f.root/'cycle-trajectory.json','One question')
        self.assertLessEqual(len(observed),8)
        self.assertTrue(any('progress_warning' in m.get('content','') for sample in observed for m in sample))

    def test_two_read_calls_parse_but_original_reviewer_stays_single_call(self):
        fixture=runtime_fixture.RuntimeTests();fixture.setUp()
        model=fixture.model(cls=ProtectedDocumentModel)
        response=self.response_batch([('search_text',{'snapshot_id':'docs-1','query':'split_after'}),('search_text',{'snapshot_id':'docs-1','query':'maxsplit'})])
        before=canonical_bytes(response.model_dump())
        self.assertEqual(len(model._parse_actions(response)),2)
        self.assertEqual(canonical_bytes(response.model_dump()),before)
        with self.assertRaises(FormatError):fixture.model()._parse_actions(response)

    def test_mixed_submission_duplicate_ids_and_executable_batches_reject(self):
        fixture=runtime_fixture.RuntimeTests();fixture.setUp();model=fixture.model(cls=ProtectedDocumentModel)
        valid=('search_text',{'snapshot_id':'docs-1','query':'split'})
        for second in [('bash',{'command':'echo forbidden'}),('submit_consultation',{})]:
            with self.subTest(second=second[0]),self.assertRaises(FormatError):model._parse_actions(self.response_batch([valid,second]))
        duplicate=self.response_batch([valid,valid]);duplicate.choices[0].message.tool_calls[1].id=duplicate.choices[0].message.tool_calls[0].id
        with self.assertRaises(FormatError):model._parse_actions(duplicate)
        with self.assertRaises(FormatError):model._parse_actions(self.response_batch([valid,valid,valid]))

    def test_two_reads_execute_serially_with_both_receipts_before_submission(self):
        f,broker=self.fixture(b'split behavior\nsecond line\n')
        doc=broker.snapshot.grant.documents[0]
        first=[('read_lines',{'snapshot_id':broker.snapshot.grant.snapshot_id,'document_id':doc.document_id,'start':1,'end':1}),
            ('search_text',{'snapshot_id':broker.snapshot.grant.snapshot_id,'query':'second'})]
        fixture=runtime_fixture.RuntimeTests();fixture.setUp();parser=fixture.model(cls=ProtectedDocumentModel)
        observed=[]
        def query(messages):
            observed.append(deepcopy(messages))
            calls=first if len(observed)==1 else [('submit_consultation',{'advice':document_fixture.advice_for(broker).model_dump(mode='json')})]
            response=self.response_batch(calls)
            return response.choices[0].message.model_dump()|{'extra':{'actions':parser._parse_actions(response)}}
        model=SimpleNamespace(query=query,usage={},contains_credentials=lambda value:False)
        model.format_observation_messages=lambda message,outputs:ReviewModel.format_observation_messages(model,message,outputs)
        result=run_document_consultation(model,broker,SimpleNamespace(check=lambda:None,identity=f.identity),f.f.root/'batch-trajectory.json','One question')
        self.assertEqual(result.status,'answered')
        self.assertEqual(len(observed),2)
        self.assertEqual([m['tool_call_id'] for m in observed[1] if m['role']=='tool'],['batch-call-0','batch-call-1'])
        self.assertEqual([e.get('tool') for e in broker.events],['read_lines','search_text','submit_consultation'])

    def test_cancel_between_serial_reads_does_not_execute_second(self):
        f,broker=self.fixture(b'split behavior\nsecond line\n');doc=broker.snapshot.grant.documents[0]
        fixture=runtime_fixture.RuntimeTests();fixture.setUp();parser=fixture.model(cls=ProtectedDocumentModel)
        response=self.response_batch([('read_lines',{'snapshot_id':broker.snapshot.grant.snapshot_id,'document_id':doc.document_id,'start':1,'end':1}),
            ('search_text',{'snapshot_id':broker.snapshot.grant.snapshot_id,'query':'second'})])
        def check():
            if broker.events:raise ReviewCancelled('cancel-between-reads')
        message=response.choices[0].message.model_dump()|{'extra':{'actions':parser._parse_actions(response)}}
        model=SimpleNamespace(query=lambda messages:message,usage={},contains_credentials=lambda value:False)
        model.format_observation_messages=lambda m,outputs:ReviewModel.format_observation_messages(model,m,outputs)
        output=f.f.root/'cancel-batch.json'
        with self.assertRaises(ReviewCancelled):run_document_consultation(model,broker,SimpleNamespace(check=check,identity=f.identity),output,'One question')
        self.assertEqual(len(broker.events),1)
        tools=[m for m in json.loads(output.read_bytes())['messages'] if m['role']=='tool']
        self.assertEqual(len(tools),2)
        self.assertEqual(json.loads(tools[1]['content'])['status'],'not_executed')

    def test_review_can_project_three_large_receipts_with_document_context(self):
        f=StageAFixture()
        f.write(('def total(values):\n'+('# documented context '+'x'*75+'\n')*125).encode())
        f.snapshot=f.seal('large-review-source')
        broker=ReviewEvidenceBroker(f.snapshot,f.reviewer,f.make_handoff(report_ids=()))
        doc=f.doc()
        messages=[{'role':'system','content':'Original reviewer instructions.'},{'role':'user','content':'Original task and version-bound document context '+ 'Q'*6500}]
        for i,start in enumerate((2,42,82)):
            args={'snapshot_id':f.snapshot.candidate.snapshot_id,'document_id':doc.document_id,'start':start,'end':start+39}
            receipt=broker.dispatch(f.reviewer,'read_lines',args)
            messages.extend([{'role':'assistant','content':'','tool_calls':[{'id':'review-call-'+str(i),'type':'function','function':{'name':'read_lines','arguments':json.dumps(args)}}]},
                {'role':'tool','tool_call_id':'review-call-'+str(i),'content':canonical_bytes(receipt).decode()}])
        raw=canonical_bytes(messages)
        fixture=runtime_fixture.RuntimeTests();fixture.setUp();model=fixture.model()
        model.review_projection_windows=(3,)
        model.review_atomic_projection=False
        old=model._prepare_messages_for_api(messages)
        model.review_projection_windows=(3,2,1)
        model.review_atomic_projection=True
        new=model._prepare_messages_for_api(messages)
        self.assertGreater(message_bytes(old),16384)
        self.assertLessEqual(message_bytes(new),16384)
        self.assertEqual(new[:2],messages[:2]);self.assertEqual(new[-1],messages[-1])
        self.assertEqual(canonical_bytes(messages),raw)
        self.assertTrue(model.review_projection_events)

    def test_review_exchange_shells_remain_bounded_across_long_histories(self):
        f=StageAFixture()
        f.write(('def total(values):\n'+('# source '+'x'*90+'\n')*90).encode())
        f.snapshot=f.seal('many-review-reads')
        broker=ReviewEvidenceBroker(f.snapshot,f.reviewer,f.make_handoff(report_ids=()))
        doc=f.doc()
        messages=[{'role':'system','content':'Original independent reviewer instructions.'},
                  {'role':'user','content':'ORIGINAL_REQUIREMENTS_AND_DOCUMENT_CONTEXT '+'Q'*8000}]
        fixture=runtime_fixture.RuntimeTests();fixture.setUp();model=fixture.model()
        for i in range(70):
            args={'snapshot_id':f.snapshot.candidate.snapshot_id,'document_id':doc.document_id,'start':i+2,'end':i+2}
            receipt=broker.dispatch(f.reviewer,'read_lines',args)
            call='bounded-review-'+str(i)
            messages.extend([{'role':'assistant','content':'','tool_calls':[{'id':call,'type':'function','function':{'name':'read_lines','arguments':json.dumps(args)}}]},
                             {'role':'tool','tool_call_id':call,'content':canonical_bytes(receipt).decode()}])
            if i+1 in (7,24,70):
                before=canonical_bytes(messages)
                projected=model._prepare_messages_for_api(messages)
                self.assertLessEqual(message_bytes(projected),16384)
                self.assertEqual(projected[:2],messages[:2])
                self.assertEqual(projected[-2:],messages[-2:])
                self.assertEqual(canonical_bytes(messages),before)
                for ordinal,message in enumerate(projected):
                    if message['role']=='tool':
                        self.assertEqual(projected[ordinal-1]['tool_calls'][0]['id'],message['tool_call_id'])
        self.assertTrue(any(e.get('mode')=='atomic_completed_exchanges' for e in model.review_projection_events))

    def test_oversized_submission_receives_size_and_can_correct_with_exact_shorter_evidence(self):
        f,broker=self.fixture(('split boundary\n' + ('long context '+'x'*190+'\n')*30).encode())
        doc=next(d for d in broker.snapshot.grant.documents if d.path=='docs/rules.md')
        for start,end in ((1,13),(14,26),(27,31)):
            broker.dispatch(f.identity,'read_lines',{'snapshot_id':broker.snapshot.grant.snapshot_id,'document_id':doc.document_id,'start':start,'end':end})
        oversized=document_fixture.advice_for(broker)
        with self.assertRaises(DocumentHandoffTooLarge) as caught:
            broker.dispatch(f.identity,'submit_consultation',{'advice':oversized.model_dump(mode='json')})
        self.assertGreater(caught.exception.payload_bytes,6144)
        self.assertIsNone(broker.advice)
        observed=[]
        def query(messages):
            observed.append(deepcopy(messages))
            if len(observed)==1:
                tool,args='submit_consultation',{'advice':oversized.model_dump(mode='json')}
            elif len(observed)==2:
                receipt=json.loads(messages[-1]['content'])
                self.assertEqual(receipt['status'],'rejected')
                self.assertEqual(receipt['reason'],'document_handoff_too_large')
                self.assertEqual(receipt['limit_bytes'],6144)
                self.assertEqual(receipt['payload_bytes'],caught.exception.payload_bytes)
                self.assertIn('narrower',receipt['correction'])
                tool,args='read_lines',{'snapshot_id':broker.snapshot.grant.snapshot_id,'document_id':doc.document_id,'start':1,'end':1}
            else:
                ref=json.loads(messages[-1]['content'])['evidence']['evidence_id']
                advice=document_fixture.advice_for(broker,refs=(ref,))
                tool,args='submit_consultation',{'advice':advice.model_dump(mode='json')}
            call='correction-'+str(len(observed))
            return {'role':'assistant','content':'','tool_calls':[{'id':call,'type':'function','function':{'name':tool,'arguments':json.dumps(args)}}],
                    'extra':{'actions':[{'tool':tool,'arguments':args,'tool_call_id':call}]}}
        model=SimpleNamespace(query=query,usage={},contains_credentials=lambda value:False)
        model.format_observation_messages=lambda m,outputs:ReviewModel.format_observation_messages(model,m,outputs)
        result=run_document_consultation(model,broker,SimpleNamespace(check=lambda:None,identity=f.identity),f.f.root/'size-correction.json','One original question')
        self.assertEqual(len(observed),3)
        self.assertEqual(result.status,'answered')
        self.assertEqual(result.snapshot_id,oversized.snapshot_id)
        self.assertEqual(len(result.evidence_ids),1)

    def test_rejection_does_not_echo_arbitrary_exception_text(self):
        for error in (ProtocolError('sensitive-value\nnot-a-code'),OSError('C:/private/file: sensitive-value')):
            result=document_rejection(error)
            self.assertNotIn('sensitive-value',json.dumps(result))
            self.assertNotIn('C:/private',json.dumps(result))
        self.assertEqual(document_rejection(ProtocolError('document_advice_unissued_evidence'))['reason'],'document_advice_unissued_evidence')

    def test_scoped_32k_review_preserves_anchors_and_recent_receipts_without_changing_defaults(self):
        from deepseek_budget import ROOT as budget_root
        config_path=budget_root/'configs/deepseek_budget.json';config_bytes=config_path.read_bytes()
        fixture=runtime_fixture.RuntimeTests();fixture.setUp()
        default=fixture.model();model=fixture.model(review_context_bytes=32768)
        self.assertEqual(default.budget_config['max_messages_utf8_bytes'],16384)
        self.assertEqual(model.budget_config['max_messages_utf8_bytes'],32768)
        self.assertEqual(model.budget_config['max_prompt_tokens'],40960)
        self.assertEqual(model.budget_config['reservation_per_attempt_cny'],'0.10')
        f=StageAFixture();f.write(('def total(values):\n'+('# source '+'x'*290+'\n')*90).encode());f.snapshot=f.seal('review-32k')
        broker=ReviewEvidenceBroker(f.snapshot,f.reviewer,f.make_handoff(report_ids=()));doc=f.doc()
        messages=[{'role':'system','content':'Original reviewer instructions.'},{'role':'user','content':'ORIGINAL_REQUIREMENTS '+'Q'*8000}]
        for i in range(18):
            args={'snapshot_id':f.snapshot.candidate.snapshot_id,'document_id':doc.document_id,'start':i+2,'end':i+7}
            receipt=broker.dispatch(f.reviewer,'read_lines',args);call='kept-'+str(i)
            messages.extend([{'role':'assistant','content':'','tool_calls':[{'id':call,'type':'function','function':{'name':'read_lines','arguments':json.dumps(args)}}]},
                             {'role':'tool','tool_call_id':call,'content':canonical_bytes(receipt).decode()}])
        original=canonical_bytes(messages);projected=model._prepare_messages_for_api(messages)
        self.assertLessEqual(message_bytes(projected),32768)
        self.assertEqual(projected[:2],messages[:2]);self.assertEqual(projected[-6:],messages[-6:])
        for value in messages[2:6]:self.assertIn(value,projected)
        self.assertEqual(canonical_bytes(messages),original);self.assertEqual(config_path.read_bytes(),config_bytes)
        with patch('review_model.litellm.completion') as transport, self.assertRaises(LimitsExceeded):
            model.query([{'role':'user','content':'x'*33000}])
        transport.assert_not_called()
        with self.assertRaises(ValueError):fixture.model(review_context_bytes=65536)
        with self.assertRaises(ValueError):fixture.model(cls=ProtectedDocumentModel,review_context_bytes=32768)

    def test_review_rotating_identical_reads_stop_with_no_new_evidence(self):
        f=StageAFixture();broker=ReviewEvidenceBroker(f.snapshot,f.reviewer,f.make_handoff(report_ids=()))
        doc=f.doc();observed=[]
        requests=[('read_diff',{'candidate_id':f.snapshot.candidate.candidate_id}),
                  ('read_lines',{'snapshot_id':f.snapshot.candidate.snapshot_id,'document_id':doc.document_id,'start':1,'end':1})]
        def query(messages):
            observed.append(deepcopy(messages));self.assertLessEqual(len(observed),10)
            tool,args=requests[(len(observed)-1)%2];call='repeat-'+str(len(observed))
            return {'role':'assistant','content':'','tool_calls':[{'id':call,'type':'function','function':{'name':tool,'arguments':json.dumps(args)}}],
                    'extra':{'actions':[{'tool':tool,'arguments':args,'tool_call_id':call}]}}
        model=SimpleNamespace(query=query,usage={},contains_credentials=lambda value:False)
        model.format_observation_messages=lambda m,outputs:ReviewModel.format_observation_messages(model,m,outputs)
        with self.assertRaisesRegex(ProtocolError,'review_repeated_evidence_limit'):
            run_review(model,broker,SimpleNamespace(check=lambda:None,identity=f.reviewer),f.root/'review-cycle.json','One immutable review')
        self.assertEqual(len(observed),7)
        self.assertTrue(any('progress_warning' in m.get('content','') for history in observed for m in history))


if __name__=='__main__':unittest.main()
