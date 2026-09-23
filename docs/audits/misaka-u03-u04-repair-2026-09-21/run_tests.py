"""Run offline pytest with isolated product/profile paths; repository import is explicit."""
from pathlib import Path
import os,sys,subprocess,tempfile,json
checkout=Path(sys.argv[1]).resolve(); label=sys.argv[2]; root=Path(__file__).parent
sandbox=Path(tempfile.mkdtemp(prefix=label+'-state-',dir=root))
env=os.environ.copy()
for key in list(env):
    if key.startswith(('MISAKA_', 'ANTHROPIC_', 'OPENAI_', 'AWS_', 'AZURE_', 'GEMINI_', 'GOOGLE_', 'MISTRAL_', 'EXA_', 'FIRECRAWL_', 'TAVILY_', 'XAI_')):
        env.pop(key)
for name,rel in {'HOME':'home','XDG_CONFIG_HOME':'config','XDG_CACHE_HOME':'cache','XDG_DATA_HOME':'data','XDG_STATE_HOME':'state','TMPDIR':'tmp','MISAKA_CODING_AGENT_DIR':'agent','MISAKA_CODING_AGENT_SESSION_DIR':'agent/sessions','MISAKA_DB':'board.db','MISAKA_MESSAGES':'messages.db','MISAKA_LCM_DB':'lcm.db','MISAKA_TASKS':'tasks','MISAKA_PAGEINDEX':'pageindex','MISAKA_NET_SOCK':'net.sock','MISAKA_NET_SNAPSHOT':'net.json','MISAKA_PROFILES':'profiles','MISAKA_WEB_CONFIG':'web.json','MISAKA_WEB_CACHE':'web-cache','MISAKA_SESSIONS':'sessions-tree'}.items():
    path=sandbox/rel; path.parent.mkdir(parents=True,exist_ok=True)
    if not path.suffix:path.mkdir(exist_ok=True)
    env[name]=str(path)
env.update(PYTHONPATH=str(checkout),MISAKA_OFFLINE='1',PYTHONDONTWRITEBYTECODE='1')
cmd=[str(checkout/'.venv/bin/python'),'-m','pytest','-q','-W','error',*sys.argv[3:]]
(root/(label+'.command.json')).write_text(json.dumps({'cmd':cmd,'cwd':str(checkout),'state':str(sandbox)},indent=2)+'\n')
with (root/(label+'.log')).open('w') as log:
    result=subprocess.run(cmd,cwd=checkout,env=env,stdout=log,stderr=subprocess.STDOUT)
print((root/(label+'.log')).read_text()[-9000:])
print('EXIT',result.returncode)
sys.exit(result.returncode)
