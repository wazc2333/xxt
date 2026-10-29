"use strict";
const $=id=>document.getElementById(id);
const states={active:"已登录",logged_out:"已退出",expired:"会话过期",interrupted:"服务中断",cancelled:"已取消"};
const jobs={queued:"等待中",running:"运行中",completed:"处理完成",partial:"部分完成",failed:"需要处理",stopped:"已停止",interrupted:"已中断"};
let csrf="",page=1,totalPages=1,timer=null,loading=false,epoch=0;
let filters={search:"",status:"",method:""};
let controlId="",controlData=null,controlTimer=null,controlEpoch=0,acting=false,controlSelection=new Set();
const terminal=new Set(["completed","partial","failed","stopped","interrupted"]);
function notice(text=""){$("admin-notice").textContent=text;$("admin-notice").hidden=!text;}
function disconnected(){closeControl();csrf="";epoch++;clearTimeout(timer);$("admin-workspace").hidden=true;$("admin-login-panel").hidden=false;$("user-rows").replaceChildren();$("admin-password").value="";}
async function api(path,body){
  const options={credentials:"same-origin",cache:"no-store",headers:{"X-Requested-With":"learning-web"}};
  if(body!==undefined){options.method="POST";options.headers["Content-Type"]="application/json";options.headers["X-CSRF-Token"]=csrf;options.body=JSON.stringify(body);}
  const response=await fetch(path,options),data=await response.json();
  if(!response.ok){if(response.status===401)disconnected();throw new Error(data.error||"请求失败");}return data;
}
async function connected(data){csrf=data.csrf;$("admin-name").textContent="管理员 · "+data.username;$("admin-login-panel").hidden=true;$("admin-workspace").hidden=false;await loadUsers();}
function date(value){return value?new Intl.DateTimeFormat("zh-CN",{timeZone:"Asia/Shanghai",year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:false}).format(new Date(value*1000)):"—";}
function cell(parent,text){const td=document.createElement("td");td.textContent=text;parent.append(td);return td;}
function small(parent,text){const node=document.createElement("small");node.textContent=text;parent.append(node);}
function render(data){
  $("known-users").textContent=data.stats.knownUsers;$("active-sessions").textContent=data.stats.activeSessions;$("running-jobs").textContent=data.stats.runningJobs;$("login-records").textContent=data.stats.loginRecords;
  $("users-total").textContent=data.total;$("user-rows").replaceChildren();$("users-empty").hidden=!!data.users.length;
  for(const user of data.users){
    const row=document.createElement("tr"),account=cell(row,""),name=document.createElement("strong");name.textContent=user.displayName;account.append(name);small(account,user.uid?"UID "+user.uid:"账号标识未获取");
    cell(row,user.loginMethod==="qr"?"扫码登录":"Cookie 登录");cell(row,date(user.loginAt));
    const active=cell(row,date(user.lastActiveAt));if(user.endedAt)small(active,"结束 "+date(user.endedAt));
    const state=cell(row,""),badge=document.createElement("span");badge.className="user-status"+(user.status==="active"?" active":"");badge.textContent=states[user.status]||user.status;state.append(badge);
    const task=cell(row,"");
    if(!user.current){task.textContent="—";}else{
      const detail=document.createElement("details"),summary=document.createElement("summary"),job=user.current.job;
      summary.textContent=job?(jobs[job.status]||job.status):user.current.busy?"正在读取课程":"暂无任务";detail.append(summary);
      const info=document.createElement("p");info.textContent=`${user.current.courseCount} 门课程 · ${user.current.jobCount} 个会话任务`;
      detail.append(info);
      if(job){const description=document.createElement("p");description.textContent=[job.currentCourse,job.currentChapter,job.currentVideo].filter(Boolean).join(" / ");detail.append(description);
        if(job.selectedChapterCount){const count=document.createElement("p");count.textContent=`已处理 ${job.processedChapters} / ${job.selectedChapterCount} 章`;detail.append(count);}}
      task.append(detail);
    }const actions=cell(row,""),manage=document.createElement("button");manage.type="button";manage.className="text-button";manage.textContent="管理";manage.addEventListener("click",()=>openControl(user.id));actions.append(manage);$("user-rows").append(row);
  }
  totalPages=Math.max(1,Math.ceil(data.total/data.pageSize));$("page-label").textContent=`${page} / ${totalPages}`;$("page-prev").disabled=page<=1;$("page-next").disabled=page>=totalPages;
  $("admin-updated").textContent="更新于 "+date(data.updatedAt)+" · 每 10 秒刷新";
}
async function loadUsers(){
  if(!csrf||loading)return;loading=true;clearTimeout(timer);const version=epoch;
  $("admin-refresh").disabled=$("filter-submit").disabled=true;
  try{let query=new URLSearchParams({...filters,page:String(page)}),data=await api("/api/admin/users?"+query);if(version===epoch&&csrf){if(page>1&&page>Math.max(1,Math.ceil(data.total/data.pageSize))){page=1;query.set("page","1");data=await api("/api/admin/users?"+query);}if(version===epoch&&csrf){render(data);notice();}}}
  catch(error){notice(error.message);}finally{loading=false;$("admin-refresh").disabled=$("filter-submit").disabled=false;if(csrf&&!document.hidden)timer=setTimeout(loadUsers,10000);}
}
$("admin-login-form").addEventListener("submit",async event=>{event.preventDefault();$("admin-login").disabled=true;notice();let password=$("admin-password").value;$("admin-password").value="";try{const pending=api("/api/admin/login",{username:$("admin-username").value.trim(),password});password="";await connected(await pending);}catch(error){notice(error.message);}finally{$("admin-login").disabled=false;}});
$("admin-logout").addEventListener("click",async()=>{try{await api("/api/admin/logout",{});disconnected();notice("已退出管理后台。");}catch(error){notice(error.message);}});
$("admin-filters").addEventListener("submit",event=>{event.preventDefault();if(loading)return;page=1;filters={search:$("user-search").value.trim(),status:$("user-status").value,method:$("user-method").value};loadUsers();});
$("admin-refresh").addEventListener("click",loadUsers);$("page-prev").addEventListener("click",()=>{if(!loading&&page>1){page--;loadUsers();}});$("page-next").addEventListener("click",()=>{if(!loading&&page<totalPages){page++;loadUsers();}});
document.addEventListener("visibilitychange",()=>{clearTimeout(timer);if(!document.hidden)loadUsers();});
function controlNotice(message=""){$("control-notice").textContent=message;$("control-notice").hidden=!message;}
function closeControl(){controlEpoch++;clearTimeout(controlTimer);controlId="";controlData=null;controlSelection.clear();$("control-key").value="";if($("user-control").open)$("user-control").close();}
function controlItems(){const catalog=controlData?.catalog;return $("control-kind").value==="homework"?(catalog?.homework||[]):(catalog?.chapters||[]);}
function enabledItem(item){return !!item&&($("control-kind").value!=="homework"||item.selectable===true);}
function controlButtons(){
  const data=controlData,locked=acting||!data?.active||data.busy,items=controlItems(),eligible=items.filter(i=>enabledItem(i)&&i.isCompleted===false);
  for(const id of ["control-key","control-key-save","control-key-clear","control-course","control-kind","control-scan","control-all","control-quiz"]){$(id).disabled=locked;}
  $("control-quiz").disabled=locked||$("control-kind").value==="homework";$("control-random").disabled=$("control-quiz").disabled||!$("control-quiz").checked;
  $("control-scan").disabled=locked||!$("control-course").value;$("control-all").disabled=locked||!eligible.length;
  const selected=items.filter(i=>controlSelection.has(i.id)&&enabledItem(i));$("control-selected").textContent=`已选 ${selected.length} 项`;
  const chosen=eligible.filter(i=>controlSelection.has(i.id)).length;$("control-all").checked=eligible.length>0&&chosen===eligible.length;$("control-all").indeterminate=selected.length>0&&!$("control-all").checked;
  $("control-start").disabled=locked||!data?.catalog?.ready||!selected.length||selected.length>($("control-kind").value==="homework"?100:1000);
  $("control-stop").disabled=acting||!data?.active||!data?.job||terminal.has(data.job.status);$("control-delete").disabled=acting||!data;
  $("control-items").querySelectorAll("input").forEach(input=>input.disabled=locked||!enabledItem(items.find(i=>i.id===input.value)));
}
function renderControl(data){
  controlData=data;$("control-title").textContent="管理 · "+data.user.displayName;$("control-account").textContent=(data.user.uid?"UID "+data.user.uid:"账号标识未获取")+" · "+(data.active?"已登录":"已退出，任务和 SK 操作不可用");
  $("control-key-state").textContent=data.active?(data.configured?"该用户已配置 SK，当前值不会返回或显示。":"该用户尚未配置 SK。"):"登录失效后不可修改凭证。";
  const course=$("control-course"),previous=course.value;course.replaceChildren();
  for(const item of data.courses){const option=document.createElement("option");option.value=item.id;option.textContent=item.name;course.append(option);}if(data.courses.some(c=>c.id===previous))course.value=previous;
  $("control-job-state").textContent=data.job?[jobs[data.job.status]||data.job.status,data.job.currentCourse,data.job.currentChapter].filter(Boolean).join(" · "):data.busy?"正在读取用户课程":"暂无任务";
  $("control-delete-info").textContent=data.user.uid?`删除此 UID 的全部 ${data.deleteRecordCount} 条登录记录，退出所有关联会话并清除保存的凭证、SK 和题库。`:`仅删除这条登录记录，退出该会话并清除保存的凭证、SK 和题库。`;
  $("control-items").replaceChildren();const items=controlItems(),valid=new Set(items.filter(enabledItem).map(i=>i.id));controlSelection=new Set([...controlSelection].filter(id=>valid.has(id)));
  for(const item of items){const row=document.createElement("label"),input=document.createElement("input"),name=document.createElement("span"),state=document.createElement("small");row.className="control-check";input.type="checkbox";input.value=item.id;input.checked=controlSelection.has(item.id);name.textContent=item.name;state.textContent=item.statusText||(item.isCompleted===true?"✓ 已完成":item.isCompleted===false?"未完成":"状态未知");input.addEventListener("change",()=>{input.checked?controlSelection.add(item.id):controlSelection.delete(item.id);controlButtons();});row.append(input,name,state);$("control-items").append(row);}
  $("control-list-state").textContent=!data.active?"":data.catalog?.ready?(items.length?`已读取 ${items.length} 项；全选只选择已确认未完成且可执行的项目。`:"列表为空。数据来自该用户当前登录。"):"请选择课程和任务类型，点击读取列表。";controlButtons();
}
async function refreshControl(initial=false){
  if(!controlId||!csrf)return;const id=controlId,version=controlEpoch;clearTimeout(controlTimer);
  const query=new URLSearchParams({kind:$("control-kind").value});if($("control-course").value)query.set("courseId",$("control-course").value);
  try{const data=await api(`/api/admin/users/${id}/control?${query}`);if(version!==controlEpoch||id!==controlId)return;renderControl(data);
    if(initial&&data.courses.length&&!data.catalog){await refreshControl();return;}
  }catch(error){if(version===controlEpoch){controlNotice(error.message);if(!csrf)return;}}
  finally{if(version===controlEpoch&&controlId&&csrf){clearTimeout(controlTimer);controlTimer=setTimeout(refreshControl,3000);}}
}
async function openControl(id){closeControl();controlId=id;controlSelection.clear();$("control-kind").value="chapters";$("control-quiz").checked=true;$("control-random").checked=false;$("control-course").replaceChildren();$("control-items").replaceChildren();$("control-title").textContent="正在读取用户…";controlNotice();controlButtons();$("user-control").showModal();await refreshControl(true);}
async function controlAction(action,body={}){
  if(!controlId||acting)return;const id=controlId,version=controlEpoch;acting=true;clearTimeout(controlTimer);controlNotice();controlButtons();
  try{const result=await api(`/api/admin/users/${id}/${action}`,body);if(version!==controlEpoch)return;
    if(action==="delete"){closeControl();await loadUsers();notice(`已删除 ${result.deletedRecords} 条登录记录，关联用户已退出。`);return;}
    controlNotice(action==="sk"?"SK 已更新，当前值不会显示。":action==="stop"?"已请求停止，等待当前网络请求结束。":action==="scan"?"正在读取列表…":"已启动所选任务。");await refreshControl();await loadUsers();
  }catch(error){if(version===controlEpoch)controlNotice(error.message);}finally{acting=false;if(controlId)controlButtons();}
}
$("control-close").addEventListener("click",closeControl);$("user-control").addEventListener("cancel",event=>{event.preventDefault();closeControl();});
$("control-key-save").addEventListener("click",()=>{const key=$("control-key").value.trim();$("control-key").value="";if(!key){controlNotice("请输入新的 SK；若需清除请点击清除 SK。");return;}controlAction("sk",{key});});
$("control-key-clear").addEventListener("click",()=>{ $("control-key").value="";controlAction("sk",{key:""});});
for(const id of ["control-course","control-kind"]){$(id).addEventListener("change",()=>{controlEpoch++;clearTimeout(controlTimer);controlSelection.clear();if(controlData)controlData.catalog=null;controlButtons();refreshControl();});}
$("control-scan").addEventListener("click",()=>{controlSelection.clear();controlAction("scan",{courseId:$("control-course").value,kind:$("control-kind").value});});
$("control-stop").addEventListener("click",()=>controlAction("stop",{jobId:controlData?.job?.id}));
$("control-quiz").addEventListener("change",controlButtons);
$("control-all").addEventListener("change",()=>{controlSelection=new Set($("control-all").checked?controlItems().filter(i=>enabledItem(i)&&i.isCompleted===false).map(i=>i.id):[]);renderControl(controlData);});
$("control-start").addEventListener("click",()=>{const kind=$("control-kind").value,body={courseId:$("control-course").value,kind};if(kind==="homework")body.workIds=[...controlSelection];else{body.chapterIds=[...controlSelection];body.quizPolicy=$("control-quiz").checked?($("control-random").checked?"random_submit":"bank_submit"):"skip";}controlAction("start",body);});
$("control-delete").addEventListener("click",()=>{if(controlData&&confirm(`删除用户“${controlData.user.displayName}”？\n${$("control-delete-info").textContent}\n此操作不能在管理页面撤销。`))controlAction("delete");});
(async()=>{try{await connected(await api("/api/admin/session"));}catch(error){if(error.message!=="请先登录管理员账号")notice(error.message);}})();
