"""Local Web UI, REST API and in-process scheduler."""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from src.app_logging import configure_logging
from src.app_logging import current_log_dir
from src.config_service import ConfigError, ConfigService
from src.environment_service import EnvironmentError, EnvironmentService
from src.jobs import JobExecutor


class SourceInput(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    xmlUrl: str = Field(min_length=8, max_length=2048)
    category: str = Field(default="Custom", max_length=100)


class SourcePatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    xmlUrl: str | None = Field(default=None, min_length=8, max_length=2048)
    category: str | None = Field(default=None, max_length=100)


class DictPayload(BaseModel):
    value: dict


class EnvironmentPayload(BaseModel):
    values: dict[str, str] = Field(default_factory=dict, max_length=32)


class PersonalPreferencesPayload(BaseModel):
    description: str = Field(default="", max_length=4_000)


class LlmSettingsPayload(BaseModel):
    model: str = Field(min_length=1, max_length=200)
    base_url: str = Field(min_length=8, max_length=2_048)
    api_key_name: str | None = Field(default=None, min_length=1, max_length=128)


class LanguagePayload(BaseModel):
    language: str = Field(pattern="^(en|zh)$")


class BulkSourcesPayload(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=50)


class SchedulerService:
    def __init__(self, config_service: ConfigService, jobs: JobExecutor):
        self.config_service = config_service
        self.jobs = jobs
        self.scheduler = AsyncIOScheduler()
        self.log = logging.getLogger("news_agent.app")
        self._config_mtime = 0.0

    def reload(self) -> None:
        self.scheduler.remove_all_jobs()
        config = self.config_service.load()
        fetch_minutes = int(config.get("schedule", {}).get("fetch_interval_minutes", 60))
        self.scheduler.add_job(self._submit_fetch, "interval", minutes=fetch_minutes, id="fetch", replace_existing=True)
        timezone_name = config.get("delivery", {}).get("timezone", "Asia/Shanghai")
        try:
            timezone = ZoneInfo(timezone_name)
        except Exception:
            timezone = ZoneInfo("UTC")
        for item in config.get("delivery", {}).get("schedules", []):
            try:
                trigger = CronTrigger.from_crontab(item["cron"], timezone=timezone)
                self.scheduler.add_job(self._submit_push, trigger, id=f"push:{item['id']}", replace_existing=True)
            except Exception as exc:
                self.log.error("invalid schedule %s: %s", item, exc)
        try:
            self._config_mtime = self.config_service.config_path.stat().st_mtime
        except OSError:
            self._config_mtime = 0.0

    async def watch_config(self) -> None:
        """Apply MCP-originated config writes without requiring a service restart."""
        while True:
            await asyncio.sleep(5)
            try:
                mtime = self.config_service.config_path.stat().st_mtime
                if mtime > self._config_mtime:
                    self.reload()
                    self.log.info("scheduler reloaded after external config update")
            except Exception as exc:
                self.log.error("could not reload scheduler configuration: %s", exc)

    async def _submit_fetch(self) -> None:
        self.jobs.submit("fetch", "schedule")

    async def _submit_push(self) -> None:
        self.jobs.submit("push", "schedule")

    def start(self) -> None:
        self.reload()
        self.scheduler.start()

    def stop(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def state(self) -> list[dict]:
        return [{"id": job.id, "next_run_time": job.next_run_time.isoformat() if job.next_run_time else None} for job in self.scheduler.get_jobs()]


def _html() -> str:
    return """<!doctype html>
<html lang=\"zh-CN\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">
<title>News Agent</title><style>:root{color:#17212b;background:#f4f6f8;font-family:Inter,ui-sans-serif,system-ui,sans-serif}*{box-sizing:border-box}body{margin:0}main{max-width:1120px;margin:0 auto;padding:32px 24px 56px}header{display:flex;justify-content:space-between;align-items:end;border-bottom:1px solid #d9e0e6;padding-bottom:20px;margin-bottom:20px}h1{font-size:26px;letter-spacing:0;margin:0}header p{margin:0;color:#5b6875}.header-actions{display:flex;align-items:center;gap:10px}.status{font-size:13px;color:#297a53;background:#e7f5eb;border:1px solid #bce1c7;padding:6px 9px;border-radius:4px}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}.wide{grid-column:1/-1}section{background:#fff;border:1px solid #d9e0e6;border-radius:6px;padding:18px}h2{font-size:15px;margin:0 0 5px}section>p,.hint{font-size:13px;line-height:1.5;color:#5b6875;margin:0 0 14px}.actions{display:flex;gap:8px;flex-wrap:wrap}button{background:#1769aa;color:#fff;border:1px solid #1769aa;border-radius:4px;padding:8px 11px;cursor:pointer;font-weight:600}button:hover{background:#11598f}button.secondary{background:#fff;color:#1769aa}button.danger{background:#fff;border-color:#b74232;color:#b74232}button.danger:hover{background:#fff4f2;color:#9c3327}.run-state{font-size:12px;color:#66727d;margin-top:10px}.logs-header{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}.compact-button{background:#fff;color:#536170;border-color:#c6d0d8;font-size:12px;padding:6px 9px}.compact-button:hover{background:#f4f8fb;color:#1769aa;border-color:#9eb8ca}label{display:block;font-size:12px;font-weight:600;color:#475461;margin:10px 0 4px}input,textarea,select{width:100%;font:13px ui-monospace,SFMono-Regular,Menlo,monospace;padding:9px;border:1px solid #bac5ce;border-radius:4px;background:#fff;color:#17212b}select{width:auto}input:focus,textarea:focus{outline:2px solid #9ec7e8;border-color:#1769aa}textarea{line-height:1.45;resize:vertical}pre{white-space:pre-wrap;max-height:440px;overflow:auto;background:#f7f9fa;border:1px solid #e2e7eb;border-radius:4px;padding:10px;font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}.env-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0 14px}.configured{color:#297a53;font-weight:500}.missing{color:#a84b2d;font-weight:500}.tabs{display:flex;gap:4px;border-bottom:1px solid #d9e0e6;margin-bottom:20px}.tab{background:transparent;color:#536170;border:0;border-bottom:2px solid transparent;border-radius:0;padding:10px 14px}.tab.active{color:#1769aa;border-bottom-color:#1769aa}.tab-panel{display:none}.tab-panel.active{display:block}.schedule-row{display:grid;grid-template-columns:1fr 110px 110px auto;gap:8px;align-items:end;border-top:1px solid #e5e9ec;padding:12px 0}.days{display:flex;gap:5px;flex-wrap:wrap}.days label{font:12px system-ui;margin:0;padding:5px;border:1px solid #c6d0d8;border-radius:4px}.days input{width:auto;margin:0 3px 0 0}.source-results,.source-list{margin-top:12px}.source-item{display:grid;grid-template-columns:minmax(160px,1fr) 2fr auto;gap:10px;padding:10px 0;border-top:1px solid #e5e9ec;font-size:13px}.source-item small{color:#66727d;overflow-wrap:anywhere}.badge{font-size:11px;padding:3px 6px;border:1px solid #c6d0d8;border-radius:4px;color:#536170;height:max-content}.result-ok{color:#297a53}.result-error{color:#b74232}@media(max-width:720px){main{padding:20px 14px 36px}header{display:block}.header-actions{margin-top:10px}header p{margin-top:8px}.grid,.env-grid{grid-template-columns:1fr}.wide{grid-column:auto}.schedule-row,.source-item{grid-template-columns:1fr}.tabs{overflow:auto}}</style></head>
<body><main><header><div><h1>News Agent</h1><p data-t=\"subtitle\">Local news collection, delivery, and agent control.</p></div><div class=\"header-actions\"><span id=\"status\" class=\"status\"></span><select id=\"language\" onchange=\"setLanguage(this.value)\"><option value=\"en\">English</option><option value=\"zh\">中文</option></select></div></header>
<nav class=\"tabs\"><button class=\"tab active\" data-tab=\"settings\" onclick=\"openTab('settings')\" data-t=\"settings\">Settings</button><button class=\"tab\" data-tab=\"sources\" onclick=\"openTab('sources')\" data-t=\"sources\">Sources</button><button class=\"tab\" data-tab=\"logs\" onclick=\"openTab('logs')\" data-t=\"logs\">Logs</button></nav>
<div id=\"settings\" class=\"tab-panel active\"><div class=\"grid\"><section class=\"wide\"><h2 data-t=\"secrets\">Model and delivery settings</h2><p data-t=\"secretsHint\">Model, delivery, and local .env values are shown below. Keep this page private.</p><div class=\"env-grid\"><label><span data-t=\"model\">Model</span> <span class=\"missing\">*</span><input id=\"llmModel\" required></label><label><span data-t=\"apiUrl\">API URL</span> <span class=\"missing\">*</span><input id=\"llmBaseUrl\" type=\"url\" required></label></div><div id=\"environment\" class=\"env-grid\"></div><div class=\"actions\"><button onclick=\"saveConnectionSettings()\" data-t=\"saveSecrets\">Save settings</button></div></section>
<section><h2>PERSONAL_PREFERENCES</h2><p data-t=\"preferencesHint\">Describe the topics, sources, style, and language you want to prioritize.</p><textarea id=\"personalPreferences\" rows=\"8\" data-t-placeholder=\"preferencesPlaceholder\"></textarea><div class=\"actions\"><button onclick=\"savePersonalPreferences()\" data-t=\"save\">Save</button></div></section>
<section><h2 data-t=\"runTitle\">Run now</h2><p data-t=\"runHint\">Fetch, score, generate, and deliver one complete news cycle.</p><div class=\"actions\"><button id=\"runButton\" onclick=\"toggleRun()\" data-t=\"run\">Run once</button></div><div id=\"runState\" class=\"run-state\"></div></section>
<section class=\"wide\"><h2 id=\"scheduleTitle\" data-t=\"schedule\">Delivery times</h2><p data-t=\"scheduleHint\">Choose one or more weekdays, a time, and the maximum number of items for each delivery.</p><div id=\"scheduleRows\"></div><div class=\"actions\"><button class=\"secondary\" onclick=\"addSchedule()\" data-t=\"addTime\">Add delivery time</button><button onclick=\"saveSchedule()\" data-t=\"saveSchedule\">Save delivery times</button></div></section></div></div>
<div id=\"sources\" class=\"tab-panel\"><section><h2 data-t=\"addSources\">Add news sources</h2><p data-t=\"sourceHint\">Paste one RSS or Atom feed URL per line. Each URL is checked before it is added.</p><textarea id=\"sourceUrls\" rows=\"7\" placeholder=\"https://example.com/feed.xml\"></textarea><div class=\"actions\"><button onclick=\"addBulkSources()\" data-t=\"verifyAdd\">Verify and add sources</button></div><div id=\"sourceResults\" class=\"source-results\"></div></section><section><h2 data-t=\"currentSources\">Current news sources</h2><p data-t=\"currentSourcesHint\">RSS feeds plus the built-in GitHub Trending and Hacker News sources.</p><input id=\"sourceSearch\" oninput=\"renderSources()\" data-t-placeholder=\"search\" placeholder=\"Search sources\"><div id=\"sourceList\" class=\"source-list\"></div></section></div>
<div id=\"logs\" class=\"tab-panel\"><section><div class=\"logs-header\"><div><h2 data-t=\"logs\">Logs</h2><p data-t=\"logsHint\">Recent application events. Secrets are not recorded.</p></div><button class=\"compact-button\" onclick=\"loadLogs()\" data-t=\"refresh\">Refresh</button></div><pre id=\"logsOutput\"></pre></section></div></main><script>
async function api(url,o={},retry=true){let token=sessionStorage.getItem('news-agent-token');let headers={'Content-Type':'application/json',...(token?{'X-News-Agent-Token':token}:{})};let r=await fetch(url,{headers,...o});if(r.status===401&&retry){let next=prompt('Enter the local API token');if(next){sessionStorage.setItem('news-agent-token',next);return api(url,o,false)}}let d=await r.json();if(!r.ok)throw Error(d.detail||'Request failed');return d}
const words={en:{subtitle:'Local news collection, delivery, and agent control.',settings:'Settings',sources:'Sources',logs:'Logs',secrets:'Model and delivery settings',secretsHint:'Model, delivery, and local .env values are shown below. Keep this page private.',saveSecrets:'Save settings',model:'Model',apiUrl:'API URL',modelApiKey:'Model API key',preferencesHint:'Describe the topics, sources, style, and language you want to prioritize.',preferencesPlaceholder:'For example: Prioritize AI agents, model releases, and practical developer tools. Prefer Chinese summaries.',save:'Save',runTitle:'Run now',runHint:'Fetch, score, generate, and deliver one complete news cycle.',run:'Run once',stop:'Stop',runningJob:'Running once now.',idleJob:'Not running.',stopping:'Stopping...',schedule:'Delivery times',scheduleHint:'Choose one or more weekdays, a time, and the maximum number of items for each delivery.',weekdays:'Weekdays',time:'Time',maxItems:'Max items',remove:'Remove',addTime:'Add delivery time',saveSchedule:'Save delivery times',addSources:'Add news sources',sourceHint:'Paste one RSS or Atom feed URL per line. Each URL is checked before it is added.',verifyAdd:'Verify and add sources',currentSources:'Current news sources',currentSourcesHint:'RSS feeds plus the built-in GitHub Trending and Hacker News sources.',search:'Search sources',logsHint:'Recent application events. Secrets are not recorded.',refresh:'Refresh',configured:'configured',missing:'not configured',saved:'Saved.',required:'Required',running:'Running',deliveryTimes:'delivery times',rssSources:'RSS sources',sourceAdded:'Added',sourceRejected:'Could not add'},zh:{subtitle:'本地新闻收集、推送与 Agent 控制。',settings:'设置',sources:'新闻来源',logs:'日志',secrets:'模型与推送设置',secretsHint:'模型、推送和本机 .env 实际值显示在下方，请勿共享此页面。',saveSecrets:'保存设置',model:'模型',apiUrl:'API 地址',modelApiKey:'模型 API Key',preferencesHint:'用一句话描述你希望优先关注的主题、来源、风格和语言。',preferencesPlaceholder:'例如：优先关注 AI Agent、模型发布和实用开发工具，中文摘要优先。',save:'保存',runTitle:'立即运行',runHint:'完成一次抓取、评分、生成和发送流程。',run:'运行一次',stop:'停止',runningJob:'正在运行本次流程。',idleJob:'当前未运行。',stopping:'正在停止...',schedule:'发送时间',scheduleHint:'为每次发送选择星期、时间和最多发送条数。',weekdays:'星期',time:'时间',maxItems:'最多条数',remove:'删除',addTime:'增加发送时间',saveSchedule:'保存发送时间',addSources:'增加新闻来源',sourceHint:'每行粘贴一个 RSS 或 Atom 地址。系统会检查通过后再添加。',verifyAdd:'检查并添加',currentSources:'已有新闻来源',currentSourcesHint:'包含 RSS，以及内置 GitHub Trending 和 Hacker News。',search:'搜索来源',logsHint:'最近应用事件，不会记录密钥。',refresh:'刷新',configured:'已配置',missing:'未配置',saved:'已保存。',required:'必填',running:'运行中',deliveryTimes:'个发送时间',rssSources:'个 RSS 来源',sourceAdded:'已添加',sourceRejected:'无法添加'}};
let lang=localStorage.getItem('news-agent-language')||'en',sourceData={rss:[],integrations:[]},currentRunJobId=null;const $=id=>document.getElementById(id);const t=k=>words[lang][k]||k;
function setLanguage(value,persist=true){lang=value;localStorage.setItem('news-agent-language',lang);$('language').value=lang;document.querySelectorAll('[data-t]').forEach(x=>x.textContent=t(x.dataset.t));document.querySelectorAll('[data-t-placeholder]').forEach(x=>x.placeholder=t(x.dataset.tPlaceholder));document.querySelectorAll('[data-t-title]').forEach(x=>{x.title=t(x.dataset.tTitle);x.setAttribute('aria-label',t(x.dataset.tTitle))});renderSchedules(readScheduleRows());renderSources();updateRunButton(currentRunJobId);if(persist)saveLanguage(value)}
async function saveLanguage(value){try{await api('/api/language',{method:'PUT',body:JSON.stringify({language:value})})}catch(e){$('status').textContent=e.message;$('status').className='status missing'}}
function openTab(name){document.querySelectorAll('.tab,.tab-panel').forEach(x=>x.classList.remove('active'));document.querySelector(`.tab[data-tab="${name}"]`).classList.add('active');$(name).classList.add('active');if(name==='logs')loadLogs()}
function escapeHtml(value){let e=document.createElement('span');e.textContent=value;return e.innerHTML}
function weekdays(){return [[1,'Mon','一'],[2,'Tue','二'],[3,'Wed','三'],[4,'Thu','四'],[5,'Fri','五'],[6,'Sat','六'],[7,'Sun','日']]}
function readScheduleRows(){return [...document.querySelectorAll('.schedule-row')].map(row=>({days:[...row.querySelectorAll('[data-day]:checked')].map(x=>Number(x.value)),time:row.querySelector('[data-time]').value,max_items:Number(row.querySelector('[data-max]').value)||8}))}
function renderSchedules(items){$('scheduleTitle').textContent=`${t('schedule')} (${items.length})`;$('scheduleRows').innerHTML=items.map(item=>`<div class="schedule-row"><div><label>${t('weekdays')}</label><div class="days">${weekdays().map(([n,en,zh])=>`<label><input data-day type="checkbox" value="${n}" ${item.days.includes(n)?'checked':''}>${lang==='zh'?zh:en}</label>`).join('')}</div></div><div><label>${t('time')}</label><input data-time type="time" value="${item.time||'08:00'}"></div><div><label>${t('maxItems')}</label><input data-max type="number" min="1" max="50" value="${item.max_items||8}"></div><button class="secondary danger" onclick="this.parentElement.remove()">${t('remove')}</button></div>`).join('')}
function addSchedule(){renderSchedules([...readScheduleRows(),{days:[1,2,3,4,5],time:'08:00',max_items:8}])}
function parseSchedule(item){let p=(item.cron||'0 8 * * *').split(/\\s+/),d=p[4]||'*',days=d==='*'?[1,2,3,4,5,6,7]:d.split(',').map(x=>x==='0'?7:Number(x)).filter(x=>x>=1&&x<=7);return{days,time:`${String(p[1]||8).padStart(2,'0')}:${String(p[0]||0).padStart(2,'0')}`,max_items:item.max_items||8}}
async function saveSchedule(){let rows=readScheduleRows();if(rows.some(x=>!x.days.length)){alert('Choose at least one weekday.');return}let schedules=rows.map((x,i)=>{let [h,m]=x.time.split(':');let days=x.days.map(d=>d===7?0:d).join(',');return{id:`delivery-${i+1}`,cron:`${m} ${h} * * ${days}`,max_items:x.max_items,sections:['rss','github','hackernews','insights']}});await api('/api/delivery',{method:'PUT',body:JSON.stringify({value:{timezone:Intl.DateTimeFormat().resolvedOptions().timeZone||'UTC',schedules}})});alert(t('saved'));load()}
function updateRunButton(runningId){currentRunJobId=runningId||null;let b=$('runButton');if(!b)return;b.textContent=currentRunJobId?t('stop'):t('run');b.classList.toggle('danger',!!currentRunJobId);$('runState').textContent=currentRunJobId?t('runningJob'):t('idleJob')}
function envDisplayName(v,c){return v.name===(c.llm.apiKeyName||'')?t('modelApiKey'):v.name}
async function load(){try{let[s,c,n,e]=await Promise.all([api('/api/status'),api('/api/config'),api('/api/news-sources'),api('/api/environment')]);if(c.output_language&&c.output_language!==lang)setLanguage(c.output_language,false);let deliveryCount=(c.delivery.schedules||[]).length;$('status').textContent=`${t('running')}: ${deliveryCount} ${t('deliveryTimes')} · ${n.rss.length} ${t('rssSources')}`;$('personalPreferences').value=c.personal_preferences||'';$('llmModel').value=c.llm.model||'';$('llmBaseUrl').value=c.llm.baseUrl||'';updateRunButton(s.jobs&&s.jobs.running?s.jobs.running.run:null);sourceData=n;renderSchedules((c.delivery.schedules||[]).map(parseSchedule));renderSources();$('environment').innerHTML=e.variables.map(v=>{let req=v.name===(c.llm.apiKeyName||'');return `<label>${escapeHtml(envDisplayName(v,c))}${req?' <span class="missing">*</span>':''} <span class="${v.configured?'configured':'missing'}">${v.configured?t('configured'):t('missing')}</span><input type="text" data-env="${v.name}" value="${escapeHtml(v.value)}" ${req?'required':''} autocomplete="off"></label>`}).join('')}catch(e){$('status').textContent=e.message;$('status').className='status missing'}}
async function toggleRun(){try{if(currentRunJobId){$('runState').textContent=t('stopping');await api('/api/jobs/'+currentRunJobId,{method:'DELETE'});setTimeout(load,400);return}let x=await api('/api/jobs/run?confirm=true',{method:'POST'});updateRunButton(x.id);setTimeout(load,400)}catch(e){alert(e.message);load()}}
async function savePersonalPreferences(){await api('/api/personal-preferences',{method:'PUT',body:JSON.stringify({description:$('personalPreferences').value})});alert(t('saved'))}
async function saveConnectionSettings(){let values={};document.querySelectorAll('[data-env]').forEach(x=>values[x.dataset.env]=x.value);if(!$('llmModel').value.trim()||!$('llmBaseUrl').value.trim()){alert(t('required'));return}let missing=[...document.querySelectorAll('[data-env][required]')].filter(x=>!x.value.trim());if(missing.length){alert(t('required'));return}try{await Promise.all([api('/api/llm-settings',{method:'PUT',body:JSON.stringify({model:$('llmModel').value,base_url:$('llmBaseUrl').value})}),api('/api/environment',{method:'PUT',body:JSON.stringify({values})})]);if(values.NEWS_AGENT_LOCAL_TOKEN)sessionStorage.setItem('news-agent-token',values.NEWS_AGENT_LOCAL_TOKEN);alert(t('saved'));load()}catch(e){alert(e.message)}}
async function saveEnvironment(){let values={};document.querySelectorAll('[data-env]').forEach(x=>{if(x.value)values[x.dataset.env]=x.value});if(!Object.keys(values).length)return;try{await api('/api/environment',{method:'PUT',body:JSON.stringify({values})});if(values.NEWS_AGENT_LOCAL_TOKEN)sessionStorage.setItem('news-agent-token',values.NEWS_AGENT_LOCAL_TOKEN);alert(t('saved'));load()}catch(e){alert(e.message)}}
async function addBulkSources(){let urls=$('sourceUrls').value.split(/\\n+/).map(x=>x.trim()).filter(Boolean);if(!urls.length)return;let r=await api('/api/sources/bulk',{method:'POST',body:JSON.stringify({urls})});$('sourceResults').innerHTML=r.results.map(x=>`<div class="source-item ${x.status==='added'?'result-ok':'result-error'}"><strong>${x.status==='added'?t('sourceAdded'):t('sourceRejected')}</strong><small>${escapeHtml(x.url)}</small><span>${escapeHtml(x.title||x.reason||'')}</span></div>`).join('');$('sourceUrls').value='';load()}
function renderSources(){let q=($('sourceSearch').value||'').toLowerCase(),items=[...sourceData.integrations,...sourceData.rss].filter(x=>`${x.name||x.title} ${x.xmlUrl||''} ${x.category||''}`.toLowerCase().includes(q));$('sourceList').innerHTML=items.map(x=>`<div class="source-item"><strong>${escapeHtml(x.name||x.title)}</strong><small>${escapeHtml(x.xmlUrl||x.kind)}</small><span class="badge">${escapeHtml(x.category||x.kind||'RSS')}</span></div>`).join('')||'<p class="hint">No sources found.</p>'}
async function loadLogs(){let l=await api('/api/logs');$('logsOutput').textContent=l.lines.join('\\n')}
$('language').value=lang;setLanguage(lang,false);load();setInterval(load,15000)</script></body></html>"""


def create_app(config_path: str | None = None) -> FastAPI:
    service = ConfigService(Path(config_path) if config_path else None)
    environment = EnvironmentService()
    config = service.load()
    configure_logging(service.paths["logs"], int(config.get("log", {}).get("retention_days", 7)))
    jobs = JobExecutor(service)
    scheduler = SchedulerService(service, jobs)
    async def require_token(x_news_agent_token: str | None = Header(default=None)) -> None:
        expected_token = os.environ.get("NEWS_AGENT_LOCAL_TOKEN")
        if expected_token and x_news_agent_token != expected_token:
            raise HTTPException(status_code=401, detail="invalid local token")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        scheduler.start()
        watcher = asyncio.create_task(scheduler.watch_config())
        try:
            yield
        finally:
            watcher.cancel()
            scheduler.stop()

    app = FastAPI(title="News Agent Local API", lifespan=lifespan)
    app.state.config_service = service
    app.state.jobs = jobs
    app.state.scheduler = scheduler
    app.state.environment_service = environment

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return _html()

    @app.get("/api/status", dependencies=[Depends(require_token)])
    async def status() -> dict:
        return {"status": "running", "config_path": str(service.config_path), "jobs": jobs.status(), "schedules": scheduler.state()}

    @app.get("/api/config", dependencies=[Depends(require_token)])
    async def get_config() -> dict:
        return service.load()

    @app.get("/api/environment", dependencies=[Depends(require_token)])
    async def get_environment() -> dict:
        return environment.status(service.load())

    @app.put("/api/environment", dependencies=[Depends(require_token)])
    async def set_environment(payload: EnvironmentPayload) -> dict:
        try:
            return environment.update(payload.values)
        except EnvironmentError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.put("/api/config/preferences", dependencies=[Depends(require_token)])
    async def set_preferences(payload: DictPayload) -> dict:
        config, revision = await service.update({"preferences": payload.value}, "web_api")
        return {"revision": revision, "preferences": config["preferences"]}

    @app.put("/api/personal-preferences", dependencies=[Depends(require_token)])
    async def set_personal_preferences(payload: PersonalPreferencesPayload) -> dict:
        try:
            config, revision = await service.update(
                {"personal_preferences": payload.description.strip()}, "web_api"
            )
            return {"revision": revision, "description": config["personal_preferences"]}
        except ConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.put("/api/llm-settings", dependencies=[Depends(require_token)])
    async def set_llm_settings(payload: LlmSettingsPayload) -> dict:
        parsed = urlparse(payload.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise HTTPException(status_code=400, detail="API URL must be an http or https URL")
        try:
            llm_patch = {"model": payload.model.strip(), "baseUrl": payload.base_url.rstrip("/")}
            if payload.api_key_name:
                llm_patch["apiKeyName"] = payload.api_key_name.strip()
            config, revision = await service.update(
                {"llm": llm_patch},
                "web_api",
            )
            return {"revision": revision, "llm": config["llm"]}
        except ConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.put("/api/language", dependencies=[Depends(require_token)])
    async def set_language(payload: LanguagePayload) -> dict:
        try:
            config, revision = await service.update({"output_language": payload.language}, "web_api")
            return {"revision": revision, "output_language": config["output_language"]}
        except ConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/sources", dependencies=[Depends(require_token)])
    async def list_sources() -> list[dict]:
        return service.sources()

    @app.post("/api/sources/verify", dependencies=[Depends(require_token)])
    async def verify_source(payload: SourceInput) -> dict:
        try:
            return await service.verify_source(payload.model_dump())
        except ConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/sources", dependencies=[Depends(require_token)])
    async def add_source(payload: SourceInput) -> dict:
        try:
            _, revision = await service.add_source(payload.model_dump(), "web_api")
            return {"revision": revision}
        except ConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/sources/bulk", dependencies=[Depends(require_token)])
    async def add_sources_in_bulk(payload: BulkSourcesPayload) -> dict:
        results = []
        for raw_url in payload.urls:
            url = raw_url.strip()
            if not url:
                continue
            title = urlparse(url).netloc or url
            source = {"title": title, "xmlUrl": url, "category": "Custom"}
            try:
                verified = await service.verify_source(source)
                source["title"] = verified["title"] or title
                _, revision = await service.add_source(source, "web_api")
                results.append({"url": url, "title": source["title"], "status": "added", "revision": revision})
            except ConfigError as exc:
                results.append({"url": url, "status": "rejected", "reason": str(exc)})
        return {"results": results}

    @app.get("/api/news-sources", dependencies=[Depends(require_token)])
    async def news_sources() -> dict:
        config = service.load()
        return {
            "rss": service.sources(config),
            "integrations": [
                {"id": "github_trending", "name": "GitHub Trending", "kind": "curated", "enabled": bool(config.get("sections", {}).get("github_trending", {}).get("enabled", False))},
                {"id": "hackernews", "name": "Hacker News", "kind": "curated", "enabled": bool(config.get("sections", {}).get("hackernews", {}).get("enabled", False))},
            ],
        }

    @app.patch("/api/sources/{source_id}", dependencies=[Depends(require_token)])
    async def patch_source(source_id: str, payload: SourcePatch) -> dict:
        try:
            _, revision = await service.update_source(source_id, payload.model_dump(exclude_none=True), "web_api")
            return {"revision": revision}
        except ConfigError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.delete("/api/sources/{source_id}", dependencies=[Depends(require_token)])
    async def delete_source(source_id: str, confirm: bool = False) -> dict:
        if not confirm:
            raise HTTPException(status_code=400, detail="pass confirm=true to remove a source")
        try:
            _, revision = await service.remove_source(source_id, "web_api")
            return {"revision": revision}
        except ConfigError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.put("/api/delivery", dependencies=[Depends(require_token)])
    async def set_delivery(payload: DictPayload) -> dict:
        try:
            config, revision = await service.update({"delivery": payload.value}, "web_api")
            scheduler.reload()
            return {"revision": revision, "delivery": config["delivery"]}
        except ConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/jobs/{kind}", dependencies=[Depends(require_token)])
    async def submit_job(kind: str, confirm: bool = False) -> dict:
        if kind not in {"fetch", "push", "preview", "run"}:
            raise HTTPException(status_code=404, detail="unknown job type")
        if kind == "run" and not confirm:
            raise HTTPException(status_code=400, detail="pass confirm=true to run fetch and delivery")
        return jobs.submit(kind, "web_api")

    @app.get("/api/jobs", dependencies=[Depends(require_token)])
    async def recent_jobs() -> list[dict]:
        return jobs.recent()

    @app.get("/api/jobs/{job_id}", dependencies=[Depends(require_token)])
    async def get_job(job_id: str) -> dict:
        record = jobs.get(job_id)
        if record is None:
            raise HTTPException(status_code=404, detail="job not found")
        return record

    @app.delete("/api/jobs/{job_id}", dependencies=[Depends(require_token)])
    async def cancel_job(job_id: str) -> dict:
        return jobs.cancel(job_id)

    @app.get("/api/logs", dependencies=[Depends(require_token)])
    async def logs(name: str = "app", lines: int = 200) -> dict:
        if name not in {"app", "fetch", "push", "web", "mcp", "audit"}:
            raise HTTPException(status_code=400, detail="unknown log name")
        path = current_log_dir(service.paths["logs"]) / f"{name}.log"
        if not path.exists():
            return {"lines": []}
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return {"lines": content[-max(1, min(lines, 1000)):]} 

    return app


def run_server(host: str = "127.0.0.1", port: int = 12301, config_path: str | None = None) -> None:
    import uvicorn
    uvicorn.run(create_app(config_path), host=host, port=port, log_level="info")
