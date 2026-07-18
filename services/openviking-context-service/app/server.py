import sys
from concurrent import futures
from pathlib import Path

import grpc

from app import config
from app.logger import log
from app.viking_store import store

GENERATED_DIR = Path(__file__).parent / "generated"
if str(GENERATED_DIR) not in sys.path:
    sys.path.insert(0, str(GENERATED_DIR))

try:
    import openviking_context_pb2
    import openviking_context_pb2_grpc
except ImportError as exc:
    raise RuntimeError("gRPC generated files not found. Run bash scripts/gen_proto.sh first.") from exc


class OpenVikingContextService(openviking_context_pb2_grpc.OpenVikingContextServicer):
    def SearchContext(self, request, context):
        result = store.search_context(
            user_id=request.user_id,
            session_id=request.session_id,
            agent_id=request.agent_id or "main",
            query=request.query,
            max_messages=request.max_messages,
            max_tokens=request.max_tokens,
            commit_limit=request.commit_limit,
        )
        return openviking_context_pb2.SearchContextResponse(
            session_summary=result["session_summary"],
            memories=[
                openviking_context_pb2.MemoryHit(
                    memory_id=item.get("memory_id", ""),
                    content=item.get("content", ""),
                    score=float(item.get("score", 0.0)),
                    token_count=int(item.get("token_count", 0)),
                )
                for item in result["memories"]
            ],
            recent_messages=[
                openviking_context_pb2.ChatMessage(role=item.get("role", ""), content=item.get("content", ""))
                for item in result["recent_messages"]
            ],
            error=result.get("error", ""),
        )

    def AppendTurn(self, request, context):
        ok, error = store.append_turn(
            user_id=request.user_id,
            session_id=request.session_id,
            agent_id=request.agent_id or "main",
            task_id=request.task_id,
            user_message=request.user_message,
            assistant_message=request.assistant_message,
            tool_summaries=list(request.tool_summaries),
            commit_limit=request.commit_limit,
        )
        return openviking_context_pb2.AppendTurnResponse(ok=ok, error=error)

    def AddSkillDocument(self, request, context):
        ok, uri, error = store.add_skill_document(
            skill_name=request.skill_name,
            version=request.version,
            content=request.content,
            source_path=request.source_path,
        )
        return openviking_context_pb2.AddSkillDocumentResponse(
            ok=ok,
            skill_name=request.skill_name,
            version=request.version,
            uri=uri,
            error=error,
        )

    def SearchSkillDocs(self, request, context):
        hits, error = store.search_skill_docs(
            query=request.query,
            skill_names=list(request.skill_names),
            top_k=request.top_k,
            max_tokens=request.max_tokens,
        )
        return openviking_context_pb2.SearchSkillDocsResponse(
            hits=[
                openviking_context_pb2.SkillDocHit(
                    skill_name=item.get("skill_name", ""),
                    doc_id=item.get("doc_id", ""),
                    chunk_id=item.get("chunk_id", ""),
                    version=item.get("version", ""),
                    title=item.get("title", ""),
                    content=item.get("content", ""),
                    token_count=int(item.get("token_count", 0)),
                    score=float(item.get("score", 0.0)),
                )
                for item in hits
            ],
            error=error,
        )

    def ListSkillDocs(self, request, context):
        names, error = store.list_skill_docs(simple=request.simple)
        return openviking_context_pb2.ListSkillDocsResponse(names=names, error=error)

    def ReadSkillDoc(self, request, context):
        ok, content, error = store.read_skill_doc(skill_name=request.skill_name, doc_type=request.doc_type or "manual")
        return openviking_context_pb2.ReadSkillDocResponse(ok=ok, content=content, error=error)


def serve():
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=16))
    openviking_context_pb2_grpc.add_OpenVikingContextServicer_to_server(OpenVikingContextService(), server)
    listen_addr = f"[::]:{config.OPENVIKING_CONTEXT_GRPC_PORT}"
    server.add_insecure_port(listen_addr)
    server.start()
    log(f"openviking-context-service started on {listen_addr}")
    log(f"viking_data dir: {config.VIKING_DATA_DIR}")
    server.wait_for_termination()
