import contextlib,sys,os,json,urllib.request
with contextlib.redirect_stdout(sys.stderr):
    from label_studio_mcp.mcp_server import mcp
@mcp.tool()
def get_company_dataset_pipeline_status() -> str:
    """Get call-intake queue counts, review readiness and quarterly training status."""
    req=urllib.request.Request('http://company-dataset-pipeline:8080/status',
        headers={'Authorization':'Bearer '+os.environ['DATASET_ADMIN_TOKEN']})
    with urllib.request.urlopen(req,timeout=15) as response:
        return json.dumps(json.load(response))
@mcp.tool()
def search_cory_company_knowledge(query: str, limit: int = 5) -> str:
    """Search only approved, current public company facts with source citations and graph relationships. Never searches customer recordings."""
    req=urllib.request.Request('http://company-dataset-pipeline:8080/knowledge/search',
        data=json.dumps({'query':query,'limit':limit}).encode(),
        headers={'Authorization':'Bearer '+os.environ['KNOWLEDGE_READ_TOKEN'],'Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=60) as response:
        return json.dumps(json.load(response))
mcp.run()
