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
from src.config import get_server_api_token
from src.config_service import ConfigError, ConfigService
from src.environment_service import EnvironmentError, EnvironmentService
from src.jobs import JobExecutor
from src.llm import check_llm_available
from src.llm_protocol import LLM_PROTOCOLS, infer_llm_protocol, resolve_llm_endpoint
from src.markdown_utils import parse_frontmatter
from src.sections.signals.collector import signal_source_catalog
from src.source_categories import normalize_source_category
from src.storage import extract_section, get_last_push_file, limit_delivery_items

SHARED_NEWS_MIN_SCORE = 60


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
    protocol: str | None = Field(default=None)


class LlmTestPayload(LlmSettingsPayload):
    api_key: str | None = Field(default=None, max_length=8_192)


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
<title>News Agent</title><style>:root{color:#17212b;background:#f4f6f8;font-family:Inter,ui-sans-serif,system-ui,sans-serif}*{box-sizing:border-box}body{margin:0}main{max-width:1120px;margin:0 auto;padding:32px 24px 56px}header{display:flex;justify-content:space-between;align-items:end;border-bottom:1px solid #d9e0e6;padding-bottom:20px;margin-bottom:20px}h1{font-size:26px;letter-spacing:0;margin:0}header p{margin:0;color:#5b6875}.header-actions{display:flex;align-items:center;gap:10px}.status{font-size:13px;color:#297a53;background:#e7f5eb;border:1px solid #bce1c7;padding:6px 9px;border-radius:4px}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}.wide{grid-column:1/-1}section{background:#fff;border:1px solid #d9e0e6;border-radius:6px;padding:18px}h2{font-size:15px;margin:0 0 5px}section>p,.hint{font-size:13px;line-height:1.5;color:#5b6875;margin:0 0 14px}.actions{display:flex;gap:8px;flex-wrap:wrap}button{background:#1769aa;color:#fff;border:1px solid #1769aa;border-radius:4px;padding:8px 11px;cursor:pointer;font-weight:600}button:hover{background:#11598f}button.secondary{background:#fff;color:#1769aa}button.danger{background:#fff;border-color:#b74232;color:#b74232}button.danger:hover{background:#fff4f2;color:#9c3327}.run-state{font-size:12px;color:#66727d;margin-top:10px}.logs-header{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}.compact-button{background:#fff;color:#536170;border-color:#c6d0d8;font-size:12px;padding:6px 9px}.compact-button:hover{background:#f4f8fb;color:#1769aa;border-color:#9eb8ca}label{display:block;font-size:12px;font-weight:600;color:#475461;margin:10px 0 4px}input,textarea,select{width:100%;font:13px ui-monospace,SFMono-Regular,Menlo,monospace;padding:9px;border:1px solid #bac5ce;border-radius:4px;background:#fff;color:#17212b}select{width:auto}input:focus,textarea:focus{outline:2px solid #9ec7e8;border-color:#1769aa}textarea{line-height:1.45;resize:vertical}pre{white-space:pre-wrap;max-height:440px;overflow:auto;background:#f7f9fa;border:1px solid #e2e7eb;border-radius:4px;padding:10px;font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}.env-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0 14px}.configured{color:#297a53;font-weight:500}.missing{color:#a84b2d;font-weight:500}.tabs{display:flex;gap:4px;border-bottom:1px solid #d9e0e6;margin-bottom:20px}.tab{background:transparent;color:#536170;border:0;border-bottom:2px solid transparent;border-radius:0;padding:10px 14px}.tab.active{color:#1769aa;border-bottom-color:#1769aa}.tab-panel{display:none}.tab-panel.active{display:block}.headline-hero{background:linear-gradient(135deg,#102a43,#1769aa);color:#fff;border:0;border-radius:10px;padding:24px;margin-bottom:16px;box-shadow:0 10px 28px rgba(16,42,67,.16)}.headline-hero h2{font-size:24px;line-height:1.2;margin:0 0 10px}.headline-hero p{color:#dbeafe;margin:0 0 12px;max-width:860px}.headline-meta{display:flex;gap:8px;flex-wrap:wrap}.pill{font-size:12px;border:1px solid rgba(255,255,255,.32);border-radius:999px;padding:4px 9px;background:rgba(255,255,255,.12)}.headline-layout{display:grid;grid-template-columns:minmax(0,1.2fr) minmax(320px,.8fr);gap:16px}.headline-card{background:#fff;border:1px solid #d9e0e6;border-radius:10px;padding:18px;box-shadow:0 6px 18px rgba(16,42,67,.05)}.headline-card h2{font-size:17px;margin-bottom:12px}.md{font-size:14px;line-height:1.65;color:#24313d}.md h2{font-size:18px;margin:18px 0 10px}.md h3{font-size:15px;margin:15px 0 6px;color:#17212b}.md p{margin:8px 0}.md a{color:#1769aa;text-decoration:none}.md a:hover{text-decoration:underline}.md table{width:100%;border-collapse:collapse;margin:10px 0;font-size:12px}.md th,.md td{border-bottom:1px solid #e5e9ec;padding:7px;text-align:left;vertical-align:top}.md hr{border:0;border-top:1px solid #e5e9ec;margin:14px 0}.schedule-row{display:grid;grid-template-columns:1fr 110px 110px auto;gap:8px;align-items:end;border-top:1px solid #e5e9ec;padding:12px 0}.days{display:flex;gap:5px;flex-wrap:wrap}.days label{font:12px system-ui;margin:0;padding:5px;border:1px solid #c6d0d8;border-radius:4px}.days input{width:auto;margin:0 3px 0 0}.source-results,.source-list{margin-top:12px}.source-item{display:grid;grid-template-columns:minmax(160px,1fr) 2fr auto;gap:10px;padding:10px 0;border-top:1px solid #e5e9ec;font-size:13px}.source-item small{color:#66727d;overflow-wrap:anywhere}.badge{font-size:11px;padding:3px 6px;border:1px solid #c6d0d8;border-radius:4px;color:#536170;height:max-content}.result-ok{color:#297a53}.result-error{color:#b74232}@media(max-width:820px){.headline-layout{grid-template-columns:1fr}}@media(max-width:720px){main{padding:20px 14px 36px}header{display:block}.header-actions{margin-top:10px}header p{margin-top:8px}.grid,.env-grid{grid-template-columns:1fr}.wide{grid-column:auto}.schedule-row,.source-item{grid-template-columns:1fr}.tabs{overflow:auto}}</style></head>
<body><style>.field-select{width:100%}.source-toolbar{display:grid;grid-template-columns:minmax(240px,1fr) minmax(180px,auto);gap:8px}.source-toolbar select{width:100%}.source-item{grid-template-columns:minmax(160px,1fr) 2fr auto auto;align-items:start}.source-results .source-item{grid-template-columns:minmax(160px,1fr) 2fr auto}.source-remove{padding:5px 8px;font-size:12px}@media(max-width:720px){.source-toolbar,.source-item,.source-results .source-item{grid-template-columns:1fr}}</style><main><header><div><h1>News Agent</h1><p data-t=\"subtitle\">Local news collection, delivery, and agent control.</p></div><div class=\"header-actions\"><span id=\"status\" class=\"status\"></span><select id=\"language\" onchange=\"setLanguage(this.value)\"><option value=\"en\">English</option><option value=\"zh\">中文</option></select></div></header>
<nav class=\"tabs\"><button class=\"tab active\" data-tab=\"headlines\" onclick=\"openTab('headlines')\" data-t=\"headlines\">Headlines</button><button class=\"tab\" data-tab=\"settings\" onclick=\"openTab('settings')\" data-t=\"settings\">Settings</button><button class=\"tab\" data-tab=\"sources\" onclick=\"openTab('sources')\" data-t=\"sources\">Sources</button><button class=\"tab\" data-tab=\"logs\" onclick=\"openTab('logs')\" data-t=\"logs\">Logs</button></nav>
<div id=\"headlines\" class=\"tab-panel active\"><div id=\"headlinesOutput\"><section><p class=\"hint\" data-t=\"headlinesEmpty\">No generated headlines yet.</p></section></div></div>
<div id=\"settings\" class=\"tab-panel\"><div class=\"grid\"><section class=\"wide\"><h2 data-t=\"secrets\">Model and delivery settings</h2><p data-t=\"secretsHint\">Model and delivery connection values are shown below. Keep this page private.</p><div class=\"env-grid\"><label><span data-t=\"model\">Model</span> <span class=\"missing\">*</span><input id=\"llmModel\" oninput=\"detectLlmProtocol()\" required></label><label><span data-t=\"apiUrl\">API URL</span> <span class=\"missing\">*</span><input id=\"llmBaseUrl\" type=\"url\" oninput=\"detectLlmProtocol()\" required></label><label><span data-t=\"protocol\">Protocol</span> <span class=\"missing\">*</span><select id=\"llmProtocol\" class=\"field-select\" onchange=\"scheduleConnectionAutoSave()\"><option value=\"openai_chat\">OpenAI Chat Completions</option><option value=\"openai_responses\">OpenAI Responses</option><option value=\"anthropic_messages\">Anthropic Messages</option></select></label><div id=\"llmApiKeyField\"></div></div><div id=\"environment\" class=\"env-grid\"></div><div class=\"actions\"><button onclick=\"saveConnectionSettings()\" data-t=\"saveSecrets\">Save settings</button><button id=\"testModelButton\" class=\"secondary\" onclick=\"testConnectionSettings()\" data-t=\"testModel\">Test model</button></div><div id=\"connectionSaveStatus\" class=\"run-state\"></div><div id=\"modelTestStatus\" class=\"run-state\"></div></section>
<section><h2>PERSONAL_PREFERENCES</h2><p data-t=\"preferencesHint\">Describe the topics, sources, style, and language you want to prioritize.</p><textarea id=\"personalPreferences\" rows=\"8\" data-t-placeholder=\"preferencesPlaceholder\"></textarea><div class=\"actions\"><button onclick=\"savePersonalPreferences()\" data-t=\"save\">Save</button></div></section>
<section><h2 data-t=\"runTitle\">Run now</h2><p data-t=\"runHint\">Fetch, score, generate, and deliver one complete news cycle.</p><div class=\"actions\"><button id=\"runButton\" onclick=\"toggleRun()\" data-t=\"run\">Run once</button></div><div id=\"runState\" class=\"run-state\"></div></section>
<section class=\"wide\"><h2 id=\"scheduleTitle\" data-t=\"schedule\">Delivery times</h2><p data-t=\"scheduleHint\">Choose one or more weekdays, a time, and the maximum number of items for each delivery.</p><div id=\"scheduleRows\"></div><div class=\"actions\"><button class=\"secondary\" onclick=\"addSchedule()\" data-t=\"addTime\">Add delivery time</button><button onclick=\"saveSchedule()\" data-t=\"saveSchedule\">Save delivery times</button></div></section></div></div>
<div id=\"sources\" class=\"tab-panel\"><section><h2 data-t=\"addSources\">Add news sources</h2><p data-t=\"sourceHint\">Paste one RSS or Atom feed URL per line. Each URL is checked before it is added.</p><textarea id=\"sourceUrls\" rows=\"7\" placeholder=\"https://example.com/feed.xml\"></textarea><div class=\"actions\"><button onclick=\"addBulkSources()\" data-t=\"verifyAdd\">Verify and add sources</button></div><div id=\"sourceResults\" class=\"source-results\"></div></section><section><h2 data-t=\"currentSources\">Current news sources</h2><p data-t=\"currentSourcesHint\">This list is the set of sources used by the next fetch.</p><div class=\"source-toolbar\"><input id=\"sourceSearch\" oninput=\"renderSources()\" data-t-placeholder=\"search\" placeholder=\"Search sources\"><select id=\"sourceCategory\" onchange=\"renderSources()\"></select></div><div id=\"sourceList\" class=\"source-list\"></div></section></div>
<div id=\"logs\" class=\"tab-panel\"><section><div class=\"logs-header\"><div><h2 data-t=\"logs\">Logs</h2><p data-t=\"logsHint\">Recent application events. Secrets are not recorded.</p></div><button class=\"compact-button\" onclick=\"loadLogs()\" data-t=\"refresh\">Refresh</button></div><pre id=\"logsOutput\"></pre></section></div></main><script>
async function api(url,o={},retry=true){let token=sessionStorage.getItem('news-agent-token');let headers={'Content-Type':'application/json',...(token?{'X-News-Agent-Token':token}:{})};let r=await fetch(url,{headers,...o});let text=await r.text();let d=null;try{d=text?JSON.parse(text):{}}catch(_){d={detail:text||r.statusText||'Request failed'}}if(r.status===401&&retry){let current=sessionStorage.getItem('news-agent-token');if(current&&current!==token)return api(url,o,false);let next=prompt('Enter the local API token');if(next){sessionStorage.setItem('news-agent-token',next);return api(url,o,false)}}if(!r.ok)throw Error(d.detail||'Request failed');return d}
const words={en:{subtitle:'Local news collection, delivery, and agent control.',settings:'Settings',headlines:'Headlines',headlinesEmpty:'No generated headlines yet.',headlineFile:'Latest generated delivery',headlineRss:'Top News',headlineGithub:'GitHub Highlights',sources:'Sources',logs:'Logs',secrets:'Model and delivery settings',secretsHint:'Model and delivery connection values are shown below. Keep this page private.',saveSecrets:'Save settings',model:'Model',apiUrl:'API URL',modelApiKey:'Model API key',preferencesHint:'Describe the topics, sources, style, and language you want to prioritize.',preferencesPlaceholder:'For example: Prioritize AI agents, model releases, and practical developer tools. Prefer Chinese summaries.',save:'Save',runTitle:'Run now',runHint:'Fetch, score, generate, and deliver one complete news cycle.',run:'Run once',stop:'Stop',runningJob:'Running once now.',idleJob:'Not running.',stopping:'Stopping...',schedule:'Delivery times',scheduleHint:'Choose one or more weekdays, a time, and the maximum number of items for each delivery.',weekdays:'Weekdays',time:'Time',maxItems:'Max items',remove:'Remove',addTime:'Add delivery time',saveSchedule:'Save delivery times',addSources:'Add news sources',sourceHint:'Paste one RSS or Atom feed URL per line. Each URL is checked before it is added.',verifyAdd:'Verify and add sources',currentSources:'Current news sources',currentSourcesHint:'RSS feeds plus built-in signal sources.',search:'Search sources',logsHint:'Recent application events. Secrets are not recorded.',refresh:'Refresh',configured:'configured',missing:'not configured',saved:'Saved.',required:'Required',running:'Running',deliveryTimes:'delivery times',rssSources:'RSS sources',sourceAdded:'Added',sourceRejected:'Could not add'},zh:{subtitle:'本地新闻收集、推送与 Agent 控制。',settings:'设置',headlines:'头条新闻',headlinesEmpty:'还没有生成过头条新闻。',headlineFile:'最新生成的推送内容',headlineRss:'头条新闻',headlineGithub:'GitHub 内容',sources:'新闻来源',logs:'日志',secrets:'模型与推送设置',secretsHint:'模型与推送连接配置显示在下方，请勿共享此页面。',saveSecrets:'保存设置',model:'模型',apiUrl:'API 地址',modelApiKey:'模型 API Key',preferencesHint:'用一句话描述你希望优先关注的主题、来源、风格和语言。',preferencesPlaceholder:'例如：优先关注 AI Agent、模型发布和实用开发工具，中文摘要优先。',save:'保存',runTitle:'立即运行',runHint:'完成一次抓取、评分、生成和发送流程。',run:'运行一次',stop:'停止',runningJob:'正在运行本次流程。',idleJob:'当前未运行。',stopping:'正在停止...',schedule:'发送时间',scheduleHint:'为每次发送选择星期、时间和最多发送条数。',weekdays:'星期',time:'时间',maxItems:'最多条数',remove:'删除',addTime:'增加发送时间',saveSchedule:'保存发送时间',addSources:'增加新闻来源',sourceHint:'每行粘贴一个 RSS 或 Atom 地址。系统会检查通过后再添加。',verifyAdd:'检查并添加',currentSources:'已有新闻来源',currentSourcesHint:'包含 RSS 和内置信号源。',search:'搜索来源',logsHint:'最近应用事件，不会记录密钥。',refresh:'刷新',configured:'已配置',missing:'未配置',saved:'已保存。',required:'必填',running:'运行中',deliveryTimes:'个发送时间',rssSources:'个 RSS 来源',sourceAdded:'已添加',sourceRejected:'无法添加'}};
Object.assign(words.en,{currentSourcesHint:'This is the active source list used by the next fetch.',allCategories:'All categories',removeSource:'Delete',confirmRemoveSource:'Remove this URL from future fetches?',sourceRemoved:'Source removed.',noSources:'No active sources found.',category_ai:'AI & Frontier Technology',category_developer_open_source:'Developers & Open Source',category_product_startup:'Products & Startups',category_business_investment:'Business & Investment',category_technology_policy:'Technology Industry & Policy',category_other:'General / Other'});Object.assign(words.zh,{currentSourcesHint:'这是下一次抓取会实际使用的来源清单。',allCategories:'全部类别',removeSource:'删除',confirmRemoveSource:'确定从后续实际抓取清单中删除这个 URL？',sourceRemoved:'来源已删除。',noSources:'没有符合条件的启用来源。',category_ai:'AI 与前沿技术',category_developer_open_source:'开发者与开源',category_product_startup:'产品与创业',category_business_investment:'商业与投资',category_technology_policy:'科技产业与政策',category_other:'综合 / 其他'});
Object.assign(words.en,{protocol:'Protocol',testModel:'Test model',testingModel:'Testing model...',modelTestOk:'Model is available'});Object.assign(words.zh,{protocol:'协议类型',testModel:'测试模型',testingModel:'正在测试模型...',modelTestOk:'模型可用'});
Object.assign(words.en,{savingSettings:'Saving...',autoSaved:'Automatically saved.'});Object.assign(words.zh,{savingSettings:'正在保存...',autoSaved:'已自动保存。'});
const sourceCategories=['ai','developer_open_source','product_startup','business_investment','technology_policy','other'],hiddenConnectionVariables=new Set(['GITHUB_TOKEN','JINA_API_KEY','NEWS_AGENT_LOCAL_TOKEN','PH_TOKEN']);let lang=localStorage.getItem('news-agent-language')||'en',sourceData={rss:[],integrations:[]},currentRunJobId=null,llmApiKeyName='OPENAI_API_KEY',connectionSaveTimer=null,connectionSaveRunning=false,connectionSaveQueued=false,connectionTestRunning=false;const $=id=>document.getElementById(id);const t=k=>words[lang][k]||k;const categoryLabel=id=>t('category_'+(id||'other'));
function setLanguage(value,persist=true){lang=value;localStorage.setItem('news-agent-language',lang);$('language').value=lang;document.querySelectorAll('[data-t]').forEach(x=>x.textContent=t(x.dataset.t));document.querySelectorAll('[data-t-placeholder]').forEach(x=>x.placeholder=t(x.dataset.tPlaceholder));document.querySelectorAll('[data-t-title]').forEach(x=>{x.title=t(x.dataset.tTitle);x.setAttribute('aria-label',t(x.dataset.tTitle))});renderSchedules(readScheduleRows());renderSources();updateRunButton(currentRunJobId);addFeishuWebhookHelp();if(persist)saveLanguage(value)}
async function saveLanguage(value){try{await api('/api/language',{method:'PUT',body:JSON.stringify({language:value})})}catch(e){$('status').textContent=e.message;$('status').className='status missing'}}
function openTab(name){document.querySelectorAll('.tab,.tab-panel').forEach(x=>x.classList.remove('active'));document.querySelector(`.tab[data-tab="${name}"]`).classList.add('active');$(name).classList.add('active');if(name==='logs')loadLogs();if(name==='headlines')loadHeadlines()}
function escapeHtml(value){let e=document.createElement('span');e.textContent=value;return e.innerHTML}
const feishuWebhookHelpCopy={en:{title:'Get a Feishu Webhook',steps:['Open the Feishu group that will receive news.','Select … → Settings → Group bots.','Select Add bot → Custom bot.','Name it News Agent and complete the setup.','Copy the generated Webhook URL and paste it into FEISHU_WEBHOOK_URL.','Save, then run once to confirm delivery.']},zh:{title:'获取飞书 Webhook',steps:['打开接收新闻的飞书群。','点击右上角 … → 设置 → 群机器人。','选择添加机器人 → 自定义机器人。','填写机器人名称，例如 News Agent，完成添加。','复制生成的 Webhook 地址，粘贴到 FEISHU_WEBHOOK_URL。','保存设置，然后运行一次确认消息发送成功。']}}
function addFeishuWebhookHelp(){let input=document.querySelector('[data-env="FEISHU_WEBHOOK_URL"]');if(!input)return;if(!$('envHelpStyles'))document.head.insertAdjacentHTML('beforeend','<style id="envHelpStyles">.env-help-field{position:relative}.env-help{display:inline-grid;place-items:center;vertical-align:middle;margin-left:5px;width:20px;height:20px;padding:0;border-radius:50%;background:#fff;color:#1769aa;border-color:#9eb8ca;font:700 12px/18px system-ui}.env-help:hover,.env-help:focus{background:#f4f8fb;color:#11598f}.env-help-tooltip{display:none;position:absolute;z-index:30;right:0;top:26px;width:min(440px,calc(100vw - 48px));padding:14px;background:#17212b;color:#fff;border-radius:6px;box-shadow:0 10px 28px rgba(16,42,67,.24);font:400 13px/1.45 system-ui}.env-help:hover+.env-help-tooltip,.env-help:focus+.env-help-tooltip,.env-help-tooltip:hover{display:block}.env-help-tooltip h3{font-size:14px;margin:0 0 9px}.env-help-tooltip ol{padding-left:20px;margin:0}.env-help-tooltip li{margin:5px 0}</style>');let label=input.closest('label');if(!label)return;label.querySelectorAll('.env-help,.env-help-tooltip').forEach(x=>x.remove());let c=feishuWebhookHelpCopy[lang]||feishuWebhookHelpCopy.en,id='feishuWebhookTooltip';label.classList.add('env-help-field');input.insertAdjacentHTML('beforebegin',`<button type="button" class="env-help" aria-describedby="${id}" aria-label="${c.title}">?</button><div id="${id}" class="env-help-tooltip" role="tooltip"><h3>${c.title}</h3><ol>${c.steps.map(x=>`<li>${x}</li>`).join('')}</ol></div>`)}
function weekdays(){return [[1,'Mon','一'],[2,'Tue','二'],[3,'Wed','三'],[4,'Thu','四'],[5,'Fri','五'],[6,'Sat','六'],[7,'Sun','日']]}
function readScheduleRows(){return [...document.querySelectorAll('.schedule-row')].map(row=>({days:[...row.querySelectorAll('[data-day]:checked')].map(x=>Number(x.value)),time:row.querySelector('[data-time]').value,max_items:Number(row.querySelector('[data-max]').value)||10}))}
function renderSchedules(items){$('scheduleTitle').textContent=`${t('schedule')} (${items.length})`;$('scheduleRows').innerHTML=items.map(item=>`<div class="schedule-row"><div><label>${t('weekdays')}</label><div class="days">${weekdays().map(([n,en,zh])=>`<label><input data-day type="checkbox" value="${n}" ${item.days.includes(n)?'checked':''}>${lang==='zh'?zh:en}</label>`).join('')}</div></div><div><label>${t('time')}</label><input data-time type="time" value="${item.time||'10:00'}"></div><div><label>${t('maxItems')}</label><input data-max type="number" min="1" max="50" value="${item.max_items||10}"></div><button class="secondary danger" onclick="this.parentElement.remove()">${t('remove')}</button></div>`).join('')}
function addSchedule(){renderSchedules([...readScheduleRows(),{days:[1,2,3,4,5,6,7],time:'10:00',max_items:10}])}
function parseSchedule(item){let p=(item.cron||'0 10 * * *').split(/\\s+/),d=p[4]||'*',days=d==='*'?[1,2,3,4,5,6,7]:d.split(',').map(x=>x==='0'?7:Number(x)).filter(x=>x>=1&&x<=7);return{days,time:`${String(p[1]||10).padStart(2,'0')}:${String(p[0]||0).padStart(2,'0')}`,max_items:item.max_items||10}}
async function saveSchedule(){let rows=readScheduleRows();if(rows.some(x=>!x.days.length)){alert('Choose at least one weekday.');return}let schedules=rows.map((x,i)=>{let [h,m]=x.time.split(':');let days=x.days.map(d=>d===7?0:d).join(',');return{id:`delivery-${i+1}`,cron:`${m} ${h} * * ${days}`,max_items:x.max_items}});await api('/api/delivery',{method:'PUT',body:JSON.stringify({value:{timezone:Intl.DateTimeFormat().resolvedOptions().timeZone||'UTC',schedules}})});alert(t('saved'));load()}
function updateRunButton(runningId){currentRunJobId=runningId||null;let b=$('runButton');if(!b)return;b.textContent=currentRunJobId?t('stop'):t('run');b.classList.toggle('danger',!!currentRunJobId);$('runState').textContent=currentRunJobId?t('runningJob'):t('idleJob')}
function envDisplayName(v,c){return v.name===(c.llm.apiKeyName||'')?t('modelApiKey'):v.name}
function envFieldHtml(v,c){let req=v.name===(c.llm.apiKeyName||'');return `<label>${escapeHtml(envDisplayName(v,c))}${req?' <span class="missing">*</span>':''} <span data-env-state class="${v.configured?'configured':'missing'}">${v.configured?t('configured'):t('missing')}</span><input type="text" data-env="${v.name}" value="${escapeHtml(v.value)}" ${req?'required':''} autocomplete="off" oninput="scheduleConnectionAutoSave()"></label>`}
function orderConnectionVariables(variables){let items=[...variables],discordIndex=items.findIndex(v=>v.name==='DISCORD_WEBHOOK_URL');if(discordIndex<0)return items;let [discord]=items.splice(discordIndex,1),feishuIndex=items.findIndex(v=>v.name==='FEISHU_WEBHOOK_URL');items.splice(feishuIndex<0?items.length:feishuIndex+1,0,discord);return items}
function detectLlmProtocol(){let endpoint=$('llmBaseUrl').value.trim(),model=$('llmModel').value.trim().toLowerCase(),path='';try{path=new URL(endpoint).pathname.toLowerCase().replace(/\\/$/,'')}catch(_){path=endpoint.toLowerCase().split(/[?#]/)[0].replace(/\\/$/,'')}let protocol=path.endsWith('/chat/completions')?'openai_chat':(path.endsWith('/responses')||path.endsWith('/response'))?'openai_responses':path.endsWith('/messages')?'anthropic_messages':/(claude|opus|sonnet|haiku)/.test(model)?'anthropic_messages':/(^|[\\/_.:-])(gpt|chatgpt|o1|o3|o4)([-_.:]|$)/.test(model)?'openai_responses':'openai_chat';$('llmProtocol').value=protocol;$('modelTestStatus').textContent='';scheduleConnectionAutoSave()}
async function load(){try{let[s,c,n,e]=await Promise.all([api('/api/status'),api('/api/config'),api('/api/news-sources'),api('/api/environment')]);if(c.output_language&&c.output_language!==lang)setLanguage(c.output_language,false);let deliveryCount=(c.delivery.schedules||[]).length;$('status').textContent=`${t('running')}: ${deliveryCount} ${t('deliveryTimes')} · ${n.rss.length} ${t('rssSources')}`;$('personalPreferences').value=c.personal_preferences||'';let active=document.activeElement,preserveConnection=connectionSaveTimer!==null||connectionSaveRunning||connectionTestRunning||['llmModel','llmBaseUrl','llmProtocol'].includes(active&&active.id)||(active&&active.hasAttribute&&active.hasAttribute('data-env'));if(!preserveConnection){$('llmModel').value=c.llm.model||'';$('llmBaseUrl').value=c.llm.baseUrl||'';$('llmProtocol').value=c.llm.protocol||'openai_chat';llmApiKeyName=c.llm.apiKeyName||'OPENAI_API_KEY';let keyVariable=hiddenConnectionVariables.has(llmApiKeyName)?null:e.variables.find(v=>v.name===llmApiKeyName);$('llmApiKeyField').innerHTML=keyVariable?envFieldHtml(keyVariable,c):'';let connectionVariables=e.variables.filter(v=>v.name!==llmApiKeyName&&!hiddenConnectionVariables.has(v.name));$('environment').innerHTML=orderConnectionVariables(connectionVariables).map(v=>envFieldHtml(v,c)).join('')}updateRunButton(s.jobs&&s.jobs.running?s.jobs.running.run:null);sourceData=n;renderSchedules((c.delivery.schedules||[]).map(parseSchedule));renderSources()}catch(e){$('status').textContent=e.message;$('status').className='status missing'}}
async function toggleRun(){try{if(currentRunJobId){$('runState').textContent=t('stopping');await api('/api/jobs/'+currentRunJobId,{method:'DELETE'});setTimeout(load,400);return}let x=await api('/api/jobs/run?confirm=true',{method:'POST'});updateRunButton(x.id);setTimeout(load,400)}catch(e){alert(e.message);load()}}
async function savePersonalPreferences(){await api('/api/personal-preferences',{method:'PUT',body:JSON.stringify({description:$('personalPreferences').value})});alert(t('saved'))}
function scheduleConnectionAutoSave(){clearTimeout(connectionSaveTimer);$('connectionSaveStatus').className='run-state';$('connectionSaveStatus').textContent=t('savingSettings');connectionSaveTimer=setTimeout(()=>{connectionSaveTimer=null;saveConnectionSettings(false)},800)}
async function saveConnectionSettings(manual=true){if(connectionSaveRunning){connectionSaveQueued=true;return false}let model=$('llmModel').value,baseUrl=$('llmBaseUrl').value,protocol=$('llmProtocol').value,values={};document.querySelectorAll('[data-env]').forEach(x=>values[x.dataset.env]=x.value);let invalid=!model.trim()||!baseUrl.trim()||[...document.querySelectorAll('[data-env][required]')].some(x=>!x.value.trim());if(invalid){$('connectionSaveStatus').textContent='';if(manual)alert(t('required'));return false}connectionSaveRunning=true;$('connectionSaveStatus').className='run-state';$('connectionSaveStatus').textContent=t('savingSettings');try{await Promise.all([api('/api/llm-settings',{method:'PUT',body:JSON.stringify({model,base_url:baseUrl,protocol})}),api('/api/environment',{method:'PUT',body:JSON.stringify({values})})]);document.querySelectorAll('[data-env]').forEach(input=>{let state=input.closest('label').querySelector('[data-env-state]'),configured=!!input.value.trim();if(state){state.className=configured?'configured':'missing';state.textContent=configured?t('configured'):t('missing')}});$('connectionSaveStatus').className='run-state result-ok';$('connectionSaveStatus').textContent=t('autoSaved');if(manual)alert(t('saved'));return true}catch(e){$('connectionSaveStatus').className='run-state result-error';$('connectionSaveStatus').textContent=e.message;if(manual)alert(e.message);return false}finally{connectionSaveRunning=false;if(connectionSaveQueued){connectionSaveQueued=false;scheduleConnectionAutoSave()}}}
async function flushConnectionAutoSave(){clearTimeout(connectionSaveTimer);connectionSaveTimer=null;while(connectionSaveRunning)await new Promise(resolve=>setTimeout(resolve,50));return saveConnectionSettings(false)}
async function testConnectionSettings(){if(!$('llmModel').value.trim()||!$('llmBaseUrl').value.trim()){alert(t('required'));return}let button=$('testModelButton'),status=$('modelTestStatus'),keyInput=document.querySelector(`[data-env="${llmApiKeyName}"]`);if(keyInput&&!keyInput.value.trim()){alert(t('required'));return}button.disabled=true;connectionTestRunning=true;status.className='run-state';status.textContent=t('testingModel');try{if(!await flushConnectionAutoSave())throw new Error(t('required'));let model=$('llmModel').value,baseUrl=$('llmBaseUrl').value,protocol=$('llmProtocol').value,result=await api('/api/llm/test',{method:'POST',body:JSON.stringify({model,base_url:baseUrl,protocol,api_key_name:llmApiKeyName,api_key:keyInput?keyInput.value:null})});status.className='run-state result-ok';status.textContent=`${t('modelTestOk')} · ${result.protocol} · ${result.endpoint}`}catch(e){status.className='run-state result-error';status.textContent=e.message}finally{connectionTestRunning=false;button.disabled=false}}
async function saveEnvironment(){let values={};document.querySelectorAll('[data-env]').forEach(x=>{if(x.value)values[x.dataset.env]=x.value});if(!Object.keys(values).length)return;try{await api('/api/environment',{method:'PUT',body:JSON.stringify({values})});if(values.NEWS_AGENT_LOCAL_TOKEN)sessionStorage.setItem('news-agent-token',values.NEWS_AGENT_LOCAL_TOKEN);alert(t('saved'));load()}catch(e){alert(e.message)}}
async function addBulkSources(){let urls=$('sourceUrls').value.split(/\\n+/).map(x=>x.trim()).filter(Boolean);if(!urls.length)return;let r=await api('/api/sources/bulk',{method:'POST',body:JSON.stringify({urls})});$('sourceResults').innerHTML=r.results.map(x=>`<div class="source-item ${x.status==='added'?'result-ok':'result-error'}"><strong>${x.status==='added'?t('sourceAdded'):t('sourceRejected')}</strong><small>${escapeHtml(x.url)}</small><span>${escapeHtml(x.title||x.reason||'')}</span></div>`).join('');$('sourceUrls').value='';load()}
function renderSources(){let category=$('sourceCategory').value||'',q=($('sourceSearch').value||'').toLowerCase(),items=[...sourceData.integrations,...sourceData.rss].filter(x=>x.enabled!==false).filter(x=>!category||x.category===category).filter(x=>`${x.name||x.title} ${x.xmlUrl||''} ${categoryLabel(x.category)}`.toLowerCase().includes(q));$('sourceCategory').innerHTML=`<option value="">${t('allCategories')}</option>`+sourceCategories.map(id=>`<option value="${id}" ${id===category?'selected':''}>${categoryLabel(id)}</option>`).join('');$('sourceList').innerHTML=items.map(x=>`<div class="source-item"><strong>${escapeHtml(x.name||x.title)}</strong><small>${escapeHtml(x.xmlUrl||'')}</small><span class="badge">${escapeHtml(categoryLabel(x.category))}</span><button class="secondary danger source-remove" onclick="removeSource('${encodeURIComponent(x.id)}')">${t('removeSource')}</button></div>`).join('')||`<p class="hint">${t('noSources')}</p>`}
async function removeSource(encodedId){if(!confirm(t('confirmRemoveSource')))return;try{await api('/api/sources/'+encodedId+'?confirm=true',{method:'DELETE'});$('status').textContent=t('sourceRemoved');await load()}catch(e){alert(e.message)}}
async function loadLogs(){let l=await api('/api/logs');$('logsOutput').textContent=l.lines.join('\\n')}
function inlineMd(s){return escapeHtml(s).replace(/\\*\\*(.*?)\\*\\*/g,'<strong>$1</strong>').replace(/\\[([^\\]]+)\\]\\(([^)]+)\\)/g,'<a href="$2" target="_blank" rel="noreferrer">$1</a>')}
function renderMarkdown(md){let lines=(md||'').trim().split(/\\n/),out=[],table=[];function flush(){if(table.length){out.push('<table>'+table.map((r,i)=>{let cells=r.split('|').slice(1,-1).map(x=>inlineMd(x.trim()));if(!cells.length)return '';if(i===1&&r.replace(/[|:\\-\\s]/g,'')==='')return '';let tag=i===0?'th':'td';return '<tr>'+cells.map(c=>`<${tag}>${c}</${tag}>`).join('')+'</tr>'}).join('')+'</table>');table=[]}}for(let raw of lines){let line=raw.trim();if(!line){flush();continue}if(line.startsWith('|')&&line.endsWith('|')){table.push(line);continue}flush();if(line==='---'){out.push('<hr>');continue}if(line.startsWith('### '))out.push('<h3>'+inlineMd(line.slice(4))+'</h3>');else if(line.startsWith('## '))out.push('<h2>'+inlineMd(line.slice(3))+'</h2>');else if(line.startsWith('- '))out.push('<p>• '+inlineMd(line.slice(2))+'</p>');else out.push('<p>'+inlineMd(line)+'</p>')}flush();return out.join('')}
async function loadHeadlines(){try{let h=await api('/api/headlines');if(!h.found){$('headlinesOutput').innerHTML=`<section><p class="hint">${t('headlinesEmpty')}</p></section>`;return}let meta=h.metadata||{};$('headlinesOutput').innerHTML=`<section class="headline-hero"><div class="headline-meta"><span class="pill">${t('headlineFile')}</span><span class="pill">${escapeHtml(h.file||'')}</span>${meta.date?`<span class="pill">${escapeHtml(meta.date)}</span>`:''}</div><h2>${escapeHtml(meta.title||'News Agent')}</h2><p>${escapeHtml(meta.lead||meta.excerpt||'')}</p></section><div class="headline-layout"><section class="headline-card"><h2>${t('headlineRss')}</h2><div class="md">${renderMarkdown(h.rss||'')}</div></section><section class="headline-card"><h2>${t('headlineGithub')}</h2><div class="md">${renderMarkdown(h.github||'')}</div></section></div>`}catch(e){$('headlinesOutput').innerHTML=`<section><p class="hint">${escapeHtml(e.message)}</p></section>`}}
new MutationObserver(addFeishuWebhookHelp).observe($('environment'),{childList:true});$('language').value=lang;setLanguage(lang,false);load().then(loadHeadlines);setInterval(load,15000)</script></body></html>"""


def create_app(config_path: str | None = None) -> FastAPI:
    service = ConfigService(Path(config_path) if config_path else None)
    environment = EnvironmentService()
    config = service.load()
    configure_logging(service.paths["logs"], int(config.get("log", {}).get("retention_days", 30)))
    jobs = JobExecutor(service)
    scheduler = SchedulerService(service, jobs)
    async def require_token(x_news_agent_token: str | None = Header(default=None)) -> None:
        expected_token = os.environ.get("NEWS_AGENT_LOCAL_TOKEN")
        if expected_token and x_news_agent_token != expected_token:
            raise HTTPException(status_code=401, detail="invalid local token")

    async def require_server_api_token(x_news_agent_token: str | None = Header(default=None)) -> None:
        config = service.load()
        expected_api = get_server_api_token(config)
        if not expected_api:
            return
        if x_news_agent_token != expected_api:
            raise HTTPException(status_code=401, detail="invalid shared news token")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        config = service.load()
        mode = config.get("mode_settings", {}).get("mode", "client")
        if mode in ("standalone", "mix"):
            from src.server_cache import server_news_cache
            data_dir = config.get("storage", {}).get("data_dir", "news-data")
            server_news_cache.initialize(data_dir, config)
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

    @app.get("/api/server/news", dependencies=[Depends(require_server_api_token)])
    async def get_server_news(hours: int = 24) -> list[dict]:
        config = service.load()
        mode = config.get("mode_settings", {}).get("mode", "client")
        if mode not in ("standalone", "mix"):
            raise HTTPException(status_code=400, detail="Server news API is only available on standalone or mix mode servers")

        from src.server_cache import server_news_cache
        return server_news_cache.get_news(
            hours,
            config,
            min_score=SHARED_NEWS_MIN_SCORE,
        )

    @app.get("/api/server/latest-digest", dependencies=[Depends(require_server_api_token)])
    async def get_server_latest_digest() -> dict:
        config = service.load()
        mode = config.get("mode_settings", {}).get("mode", "client")
        if mode not in ("standalone", "mix"):
            raise HTTPException(status_code=400, detail="Server latest-digest API is only available on standalone or mix mode servers")

        data_dir = config.get("storage", {}).get("data_dir", "news-data")
        latest = get_last_push_file(data_dir)
        if not latest:
            return {"found": False}

        path = Path(latest)
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
            metadata, body = parse_frontmatter(content)
            title = metadata.get("title", "")
            return {
                "found": True,
                "title": title,
                "content": body,
                "metadata": metadata
            }
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to read latest push file: {exc}") from exc

    @app.get("/api/server/github-trending", dependencies=[Depends(require_server_api_token)])
    async def get_server_github_trending() -> dict:
        config = service.load()
        mode = config.get("mode_settings", {}).get("mode", "client")
        if mode not in ("standalone", "mix"):
            raise HTTPException(status_code=400, detail="Server github-trending API is only available on standalone or mix mode servers")

        data_dir = config.get("storage", {}).get("data_dir", "news-data")
        from src.storage import load_github_cache
        cached = load_github_cache(data_dir)
        if cached:
            return {"found": True, "github": cached}

        latest = get_last_push_file(data_dir)
        if not latest:
            return {"found": False}

        path = Path(latest)
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
            metadata, body = parse_frontmatter(content)
            github = extract_section(body, "github").strip()
            return {
                "found": True,
                "github": github
            }
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to read latest push file for GitHub Trending: {exc}") from exc

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
            protocol = payload.protocol or infer_llm_protocol(
                payload.base_url, payload.model
            )
            if protocol not in LLM_PROTOCOLS:
                raise HTTPException(status_code=400, detail="Unsupported LLM protocol")
            llm_patch = {
                "model": payload.model.strip(),
                "baseUrl": payload.base_url.strip().rstrip("/"),
                "protocol": protocol,
            }
            if payload.api_key_name:
                llm_patch["apiKeyName"] = payload.api_key_name.strip()
            config, revision = await service.update(
                {"llm": llm_patch},
                "web_api",
            )
            return {"revision": revision, "llm": config["llm"]}
        except ConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/llm/test", dependencies=[Depends(require_token)])
    async def test_llm_settings(payload: LlmTestPayload) -> dict:
        parsed = urlparse(payload.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise HTTPException(status_code=400, detail="API URL must be an http or https URL")
        protocol = payload.protocol or infer_llm_protocol(
            payload.base_url, payload.model
        )
        if protocol not in LLM_PROTOCOLS:
            raise HTTPException(status_code=400, detail="Unsupported LLM protocol")
        current = service.load().get("llm", {})
        test_config = {
            **current,
            "model": payload.model.strip(),
            "baseUrl": payload.base_url.strip().rstrip("/"),
            "protocol": protocol,
            "apiKeyName": payload.api_key_name or current.get("apiKeyName", "OPENAI_API_KEY"),
        }
        try:
            response = await check_llm_available(
                test_config, api_key=payload.api_key
            )
            return {
                "ok": True,
                "protocol": protocol,
                "endpoint": resolve_llm_endpoint(test_config["baseUrl"], protocol),
                "response": response[:200],
            }
        except Exception as exc:
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
            source = {"title": title, "xmlUrl": url, "category": "other"}
            try:
                verified = await service.verify_source(source)
                source["title"] = verified["title"] or title
                source["category"] = normalize_source_category(
                    None, title=source["title"], url=url
                )
                _, revision = await service.add_source(source, "web_api")
                results.append({"url": url, "title": source["title"], "status": "added", "revision": revision})
            except ConfigError as exc:
                results.append({"url": url, "status": "rejected", "reason": str(exc)})
        return {"results": results}

    def news_sources_payload() -> dict:
        config = service.load()
        return {
            "rss": service.sources(config),
            "integrations": [
                {"id": "github_trending", "name": "GitHub Trending", "xmlUrl": "https://github.com/trending", "kind": "curated", "category": "developer_open_source", "enabled": bool(config.get("sections", {}).get("github_trending", {}).get("enabled", False))},
                {"id": "hackernews", "name": "Hacker News", "xmlUrl": "https://news.ycombinator.com", "kind": "curated", "category": "developer_open_source", "enabled": bool(config.get("sections", {}).get("hackernews", {}).get("enabled", False))},
                *[
                    {
                        **source,
                        "enabled": (
                            bool(config.get("sections", {}).get("google_news", {}).get("enabled", False))
                            if source["id"].startswith("google-news-")
                            else bool(config.get("sections", {}).get("signals", {}).get("enabled", False))
                        )
                        and bool(config.get("sections", {}).get("signals", {}).get("sources", {}).get(source["id"], True)),
                    }
                    for source in signal_source_catalog()
                ],
            ],
        }

    @app.get("/api/news-sources", dependencies=[Depends(require_token)])
    async def news_sources() -> dict:
        return news_sources_payload()

    @app.get("/api/server/sources", dependencies=[Depends(require_server_api_token)])
    async def shared_news_sources() -> dict:
        config = service.load()
        mode = config.get("mode_settings", {}).get("mode", "client")
        if mode not in ("standalone", "mix"):
            raise HTTPException(
                status_code=400,
                detail="Server sources API is only available on standalone or mix mode servers",
            )
        return news_sources_payload()

    @app.get("/api/headlines", dependencies=[Depends(require_token)])
    async def headlines() -> dict:
        config = service.load()
        data_dir = config.get("storage", {}).get("data_dir", "news-data")
        latest = get_last_push_file(data_dir)
        if not latest:
            return {"found": False}
        path = Path(latest)
        content = path.read_text(encoding="utf-8", errors="replace")
        metadata, body = parse_frontmatter(content)
        rss = extract_section(body, "rss").strip()
        github = extract_section(body, "github").strip()
        hackernews = extract_section(body, "hackernews").strip()
        normalized = limit_delivery_items(
            {"rss": rss, "github": github, "hackernews": hackernews},
            max_items=10,
            github_max_items=3,
        )
        rss = normalized.get("rss", rss)
        github = normalized.get("github", github)
        if not rss and not github:
            rss = body.strip()
        return {
            "found": True,
            "file": path.name,
            "metadata": metadata,
            "rss": rss,
            "github": github,
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
