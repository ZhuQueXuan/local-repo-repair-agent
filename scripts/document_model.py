"""Document specialist reuses the existing provider, guards and shared ledger."""
from collaboration_runtime import ProtectedReviewModel
from document_agent import DOCUMENT_SCHEMAS
import json
from collaboration_protocol import ProtocolError, canonical_bytes, sha256
from context_budget import message_bytes
from minisweagent.exceptions import FormatError


class ProtectedDocumentModel(ProtectedReviewModel):
    tool_schemas = DOCUMENT_SCHEMAS
    review_projection_windows = (3,)  # Own atomic-group projection follows.
    review_atomic_projection = False  # Keep indices paired for its two-read groups.

    def __init__(self, *args, **kwargs):
        if kwargs.pop("review_context_bytes", 16384) != 16384:
            raise ValueError("Document lookup keeps the original 16KiB bound")
        super().__init__(*args, **kwargs)

    def _parse_actions(self, response):
        if self.contains_credentials(canonical_bytes(response.model_dump()).decode()):
            raise ProtocolError('credential_in_model_output')
        calls=response.choices[0].message.tool_calls or []
        names=[c.function.name for c in calls]
        if not 1<=len(calls)<=2 or len({c.id for c in calls})!=len(calls) or ('submit_consultation' in names and len(calls)!=1):
            raise FormatError({'role':'user','content':'Use one or two registered READ tools per response, or submit_consultation alone. Read calls are executed serially, never concurrently.',
                'extra':{'interrupt_type':'FormatError','document_batch_rejection':{'call_count':len(calls),'tools':names[:3]}}})
        # Validate every call before any read can run. No mixed submission/read
        # batches, executable tools or discarded extra actions are accepted.
        actions=[]
        if sum(len((c.function.arguments or '').encode()) for c in calls)>32768:
            raise FormatError({'role':'user','content':'Document batch arguments exceed the bounded size.'})
        for call in calls:
            single=response.model_copy(deep=True)
            single.choices[0].message.tool_calls=[call]
            actions.extend(super()._parse_actions(single))
        return actions

    def _prepare_messages_for_api(self, messages):
        prepared = super()._prepare_messages_for_api(messages)
        if message_bytes(prepared) <= 12288:
            return prepared
        groups=[]
        for i,m in enumerate(prepared):
            calls=m.get('tool_calls',[]) if m.get('role')=='assistant' else []
            if calls and i+len(calls)<len(prepared) and all(prepared[i+j+1].get('role')=='tool' and prepared[i+j+1].get('tool_call_id')==call['id'] for j,call in enumerate(calls)):
                groups.append(tuple(range(i,i+len(calls)+1)))
        # Remove only completed assistant/tool pairs together. The raw messages
        # remain in the durable trajectory; no synthetic tool receipt is added.
        for keep in (3, 2, 1):
            archived = groups[:-keep]
            if not archived:
                continue
            omitted = {index for group in archived for index in group}
            evidence_ids = []
            excerpts, requests = {}, {}
            for i in [index for group in archived for index in group[1:]]:
                raw = json.loads(messages[i]["content"])
                evidence_ids.extend([raw["evidence"]["evidence_id"]] if raw.get("evidence") else [])
                evidence_ids.extend(hit["evidence"]["evidence_id"] for hit in raw.get("hits", []))
                for hit in ([raw] if raw.get("evidence") else raw.get("hits", [])):
                    ref=hit["evidence"]
                    excerpts[ref["evidence_id"]]={"evidence_id":ref["evidence_id"],"document_id":ref.get("document_id"),
                        "start_line":ref.get("start_line"),"end_line":ref.get("end_line"),
                        "exact_prefix":hit.get("content","")[:180],"prefix_is_full_quote":len(hit.get("content",""))<=180}
                assistant_index=next(group[0] for group in archived if i in group)
                call=next(c['function'] for c in messages[assistant_index]['tool_calls'] if c['id']==messages[i]['tool_call_id'])
                requests[call["name"]+call["arguments"]]={"tool":call["name"],"arguments":json.loads(call["arguments"])}
            note = {"completed_document_exchanges_archived": len(archived),
                "archive_sha256": sha256(canonical_bytes([[messages[i] for i in group] for group in archived])),
                "recent_issued_evidence_ids": list(dict.fromkeys(evidence_ids))[-20:],
                "recent_unique_requests": list(requests.values())[-6:],
                "issued_quote_prefixes": list(excerpts.values())[-6:],
                "scope": "API view only. Full messages/quotes remain in the trajectory. Re-read registered IDs for exact text; do not infer missing quotations."}
            projected = [m for i,m in enumerate(prepared) if i not in omitted]
            projected.insert(2, {"role": "user", "content": canonical_bytes(note).decode()})
            if keep == 1:
                # Historic receipts may predate the complete-envelope limit.
                # Preserve the newest real receipt; shrink optional previews
                # before allowing the unchanged hard guard to decide.
                while message_bytes(projected) > 12288 and note["issued_quote_prefixes"]:
                    note["issued_quote_prefixes"].pop(0)
                    projected[2] = {"role":"user","content":canonical_bytes(note).decode()}
            if not hasattr(self, "document_projection_events"):
                self.document_projection_events = []
            if message_bytes(projected) <= 12288 or keep == 1:
                self.document_projection_events.append({"before_bytes": message_bytes(prepared), "after_bytes": message_bytes(projected),
                    "archived_pairs": len(archived), "sha256": note["archive_sha256"]})
                return projected
        return prepared  # The original 16KiB guard still rejects irreducible input.
