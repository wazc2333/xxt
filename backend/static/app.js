"use strict";
const $=id=>document.getElementById(id);
const terminal=new Set(["completed","partial","failed","stopped","interrupted"]);
const labels={queued:"等待中",running:"运行中",completed:"处理完成",partial:"部分完成",failed:"需要处理",stopped:"已停止",interrupted:"已中断"};
let csrf="",courses=[],chapters=[],selectedCourse="",current=null,timer=null,busy=false,generation=0,ready=false;
let chapterProgress=null;
const selections=new Map();
let courseMode="chapters",homeworkRows=[],homeworkReady=false,homeworkSettings=null;
const homeworkSelections=new Map();
let loginMode="qr",qrEpoch=0,qrTimer=null,qrCountdown=null,qrRequest=null,qrDeadline=0,loginTransition=false;
function clearQr(){qrEpoch++;clearTimeout(qrTimer);clearInterval(qrCountdown);$("qr-image").hidden=true;$("qr-image").removeAttribute("src");$("qr-placeholder").hidden=false;$("qr-expiry").textContent="";}
async function cancelQr(){
  clearQr();const pending=qrRequest;
  if(pending){try{await pending;}catch{}}
  try{await api("/api/login/qr/cancel",{});}catch{}
}
function qrState(data){
  $("qr-status").textContent=data.message;qrDeadline=Date.now()+1000*(data.expiresIn||0);
  if(data.status!=="waiting"&&data.status!=="scanned"){
    clearTimeout(qrTimer);clearInterval(qrCountdown);$("qr-image").hidden=true;$("qr-image").removeAttribute("src");$("qr-placeholder").hidden=false;$("qr-placeholder").textContent=data.status==="expired"?"二维码已过期":"请刷新二维码";$("qr-expiry").textContent="";
  }
}
async function beginQr(){
  if(csrf||loginMode!=="qr")return;notice();clearQr();const epoch=qrEpoch;
  $("qr-refresh").disabled=true;$("qr-placeholder").textContent="正在获取二维码…";$("qr-status").textContent="正在连接超星登录服务…";
  try{
    const pending=api("/api/login/qr",{});qrRequest=pending;const data=await pending;
    if(epoch!==qrEpoch||loginMode!=="qr"||csrf)return;
    if(typeof data.image!=="string"||!/^data:image\/(png|jpeg);base64,/.test(data.image))throw new Error("未获得有效二维码，请刷新重试");
    $("qr-image").src=data.image;$("qr-image").hidden=false;$("qr-placeholder").hidden=true;qrState(data);
    qrCountdown=setInterval(()=>{
      if(epoch!==qrEpoch)return;
      const seconds=Math.max(0,Math.ceil((qrDeadline-Date.now())/1000));$("qr-expiry").textContent=`有效期剩余 ${format(seconds)}`;
      if(!seconds)qrState({status:"expired",message:"二维码已过期，请刷新二维码"});
    },1000);
    qrTimer=setTimeout(()=>pollQr(epoch),1000*(data.pollAfter||3));
  }catch(error){if(epoch===qrEpoch){$("qr-status").textContent=error.message;$("qr-placeholder").textContent="二维码获取失败";}}
  finally{qrRequest=null;if(epoch===qrEpoch)$("qr-refresh").disabled=false;}
}
async function pollQr(epoch){
  if(epoch!==qrEpoch||loginMode!=="qr"||csrf)return;
  try{
    const pending=api("/api/login/qr/status",{});qrRequest=pending;const data=await pending;
    if(epoch!==qrEpoch||loginMode!=="qr"||csrf)return;
    if(data.status==="authenticated"){await connected(data);return;}
    qrState(data);if(data.status==="waiting"||data.status==="scanned")qrTimer=setTimeout(()=>pollQr(epoch),1000*(data.pollAfter||3));
  }catch(error){if(epoch===qrEpoch){clearQr();$("qr-status").textContent=error.message;$("qr-placeholder").textContent="请刷新二维码";}}
  finally{qrRequest=null;}
}
async function setLoginMode(mode){
  if(loginTransition||mode===loginMode)return;loginTransition=true;loginMode=mode;$("qr-tab").disabled=$("cookie-tab").disabled=true;
  $("qr-tab").setAttribute("aria-selected",String(mode==="qr"));$("cookie-tab").setAttribute("aria-selected",String(mode==="cookie"));$("qr-tab").tabIndex=mode==="qr"?0:-1;$("cookie-tab").tabIndex=mode==="cookie"?0:-1;
  $("qr-panel").hidden=mode!=="qr";$("login-form").hidden=mode!=="cookie";$("cookie").value="";
  try{await cancelQr();if(mode==="qr")await beginQr();}
  finally{loginTransition=false;$("qr-tab").disabled=$("cookie-tab").disabled=false;$("qr-refresh").disabled=false;}
}
function notice(message=""){$("notice").textContent=message;$("notice").hidden=!message;}
async function api(path,body){
  const options={credentials:"same-origin",cache:"no-store",headers:{"X-Requested-With":"learning-web"}};
  if(body!==undefined){options.method="POST";options.headers["Content-Type"]="application/json";options.headers["X-CSRF-Token"]=csrf;options.body=JSON.stringify(body);}
  const response=await fetch(path,options),data=await response.json();
  if(!response.ok){if(response.status===401)disconnected();throw new Error(data.error||"请求失败");}return data;
}
function disconnected(){
  clearQr();$("qr-placeholder").textContent="点击刷新二维码开始登录";$("qr-refresh").disabled=false;
  clearTimeout(timer);generation++;csrf="";current=null;courses=[];chapters=[];selectedCourse="";busy=false;ready=false;selections.clear();
  chapterProgress=null;
  homeworkRows=[];homeworkReady=false;homeworkSettings=null;homeworkSelections.clear();$("deepseek-key").value="";$("quiz-random").checked=false;
  $("workspace").hidden=true;$("login-panel").hidden=false;$("connection").textContent="未登录";
}
function requestedCourse(){try{return location.hash.startsWith("#course/")?decodeURIComponent(location.hash.slice(8).split("/")[0]):"";}catch{return "";}}
async function connected(data){
  notice();clearQr();
  csrf=data.csrf;courses=data.courses;$("login-panel").hidden=true;$("workspace").hidden=false;$("connection").textContent="已连接";
  const jobs=data.jobs||[];current=jobs.find(j=>!terminal.has(j.status))||jobs.at(-1)||null;
  renderCourses();renderJob();
  const resume=current&&!terminal.has(current.status)?current.courses[0]?.id:requestedCourse();
  if(resume&&courses.some(c=>c.id===resume)){
    const work=(current&&!terminal.has(current.status)&&current.kind?.startsWith("homework"))||location.hash.endsWith("/homework");
    history.replaceState(null,"",`#course/${encodeURIComponent(resume)}${work?"/homework":""}`);await chooseCourse(resume,false,true);
  }else showCourseList();
  if(current&&!terminal.has(current.status))schedule();
}
function showCourseList(){$("course-list-page").hidden=false;$("course-page").hidden=true;buttons();}
function renderCourses(){
  $("courses").replaceChildren();$("course-count").textContent=courses.length;
  for(const course of courses){
    const row=document.createElement("button");row.type="button";row.className="course-entry";
    const name=document.createElement("span"),arrow=document.createElement("span");name.textContent=course.name;arrow.textContent="→";arrow.setAttribute("aria-hidden","true");row.append(name,arrow);
    row.addEventListener("click",()=>{history.pushState(null,"",`#course/${encodeURIComponent(course.id)}`);chooseCourse(course.id);});$("courses").append(row);
  }
  if(!courses.length){const p=document.createElement("p");p.textContent="当前没有可选课程";p.className="muted";$("courses").append(p);}buttons();
}
function selected(){return [...(selections.get(selectedCourse)||[])].filter(id=>chapters.some(ch=>ch.id===id));}
function incompleteChapters(){return chapters.filter(ch=>ch.isCompleted===false);}
function incompleteHomework(){return homeworkRows.filter(r=>r.selectable&&r.isCompleted===false);}
function allSelection(input,eligible,chosen){
  const count=eligible.filter(r=>chosen.includes(r.id)).length;
  input.checked=eligible.length>0&&count===eligible.length;
  input.indeterminate=chosen.length>0&&!input.checked;
}
function renderChapters(){
  const checked=selections.get(selectedCourse)||new Set();selections.set(selectedCourse,checked);
  $("chapters").replaceChildren();$("chapter-count").textContent=chapters.length;$("chapter-picker").hidden=!chapters.length;
  for(const chapter of chapters){
    const row=document.createElement("label");row.className="chapter";
    const input=document.createElement("input");input.type="checkbox";input.value=chapter.id;input.checked=checked.has(chapter.id);
    input.addEventListener("change",()=>{const chosen=selections.get(selectedCourse);input.checked?chosen.add(chapter.id):chosen.delete(chapter.id);buttons();});
    const name=document.createElement("span");name.textContent=chapter.name;row.append(input,name);$("chapters").append(row);
    name.className="chapter-title";
    const state=document.createElement("span");state.className="chapter-state";
    if(chapter.isCompleted===true){state.classList.add("complete");state.textContent="✓ 已完成";}
    else if(chapter.isCompleted===false){state.classList.add("incomplete");state.textContent=Number.isInteger(chapter.remainingTasks)?`待完成 ${chapter.remainingTasks} 个任务点`:"未完成";}
    else{state.classList.add("unknown");state.textContent="状态未知";}
    state.title="状态来自服务器章节目录；点击重读目录可更新。";row.append(state);
  }
  const done=chapters.filter(ch=>ch.isCompleted===true).length,known=chapters.some(ch=>typeof ch.isCompleted==="boolean");
  const progress=chapterProgress,hasProgress=progress&&Number.isInteger(progress.completed)&&Number.isInteger(progress.total)&&progress.completed>=0&&progress.completed<=progress.total;
  $("chapter-progress").hidden=!chapters.length||(!hasProgress&&!known);
  $("chapter-progress-text").textContent=(hasProgress?`已完成任务点 ${progress.completed} / ${progress.total} · `:"")+`已完成章节 ${done} / ${chapters.length}`;
  $("chapter-progress-bar").hidden=!hasProgress||progress.total===0;
  if(hasProgress){$("chapter-progress-bar").max=Math.max(1,progress.total);$("chapter-progress-bar").value=progress.completed;}
  buttons();
}
function renderCatalog(catalog){
  if(catalog.courseId!==selectedCourse)return;
  chapters=catalog.chapters||[];chapterProgress=catalog.taskProgress||null;ready=catalog.ready===true;renderChapters();
  $("chapters-hint").textContent=ready?"完成标记来自服务器目录，点击“重读目录”可更新。全选仅选择已确认未完成的章节；状态未知可手动选择。执行到所选章节时才读取任务详情。":"正在读取章节目录…";
}
async function chooseCourse(id,force=false,restoring=false){
  if(!restoring&&(busy||(current&&!terminal.has(current.status))))return;
  const course=courses.find(c=>c.id===id);if(!course)return;
  selectedCourse=id;chapters=[];chapterProgress=null;ready=false;$("course-list-page").hidden=true;$("course-page").hidden=false;$("course-title").textContent=course.name;
  courseMode=(restoring&&current&&!terminal.has(current.status)&&current.kind?.startsWith("homework"))||location.hash.endsWith("/homework")?"homework":"chapters";setCoursePanels();
  if(courseMode==="homework"){await loadHomework(force,restoring);return;}
  $("chapters-hint").textContent="正在读取章节目录…";renderChapters();
  busy=true;buttons();notice();const version=generation;
  try{
    await loadHomeworkSettings();if(version!==generation||id!==selectedCourse)return;
    const catalog=force?null:await api(`/api/courses/${id}/tasks`);if(version!==generation)return;
    if(catalog?.ready)renderCatalog(catalog);
    else if(!restoring||!current||terminal.has(current.status)){
      current=await api(`/api/courses/${id}/tasks/scan`,{scope:"outline"});if(version!==generation)return;renderJob();
      if(terminal.has(current.status)){
        if(current.status==="completed")renderCatalog(await api(`/api/courses/${id}/tasks`));
        else $("chapters-hint").textContent="目录读取未完成，请重新读取。";
      }else schedule();
    }
    if(restoring&&current?.courses[0]?.id===id&&current?.selectedChapterIds?.length){selections.set(id,new Set(current.selectedChapterIds));$("quiz-submit").checked=current.quizPolicy!=="skip";$("quiz-random").checked=current.quizPolicy==="random_submit";renderChapters();}
  }catch(error){notice(error.message);$("chapters-hint").textContent="目录读取未完成，请重新读取。";}
  finally{if(version===generation){busy=false;buttons();}}
}
function buttons(){
  const running=current&&!terminal.has(current.status),locked=busy||running,count=selected().length;
  $("selected-count").textContent=`已选 ${count} 章`;$("start").disabled=locked||!ready||!count||count>1000;
  $("refresh").disabled=locked;$("rescan").disabled=locked||!selectedCourse;$("stop").disabled=busy||!running;
  const incomplete=incompleteChapters();
  $("select-all").disabled=locked||!ready||!incomplete.length;$("quiz-submit").disabled=locked;
  $("quiz-random").disabled=locked||!$("quiz-submit").checked;
  allSelection($("select-all"),incomplete,selected());
  $("courses").querySelectorAll("button").forEach(n=>n.disabled=locked);
  $("chapters").querySelectorAll("input").forEach(n=>n.disabled=locked||!ready);
  for(const id of ["chapters-tab","homework-tab","homework-refresh","deepseek-save","deepseek-clear","deepseek-key","bank-file","bank-export"]){$(id).disabled=locked;}
  const chosen=selectedHomework().length,available=incompleteHomework();
  $("homework-start").disabled=locked||!homeworkReady||!chosen||chosen>100;$("homework-all").disabled=locked||!homeworkReady||!available.length;
  $("homework-selected").textContent=`已选 ${chosen} 份`;allSelection($("homework-all"),available,selectedHomework());
  $("homework-list").querySelectorAll("input").forEach(n=>n.disabled=locked||!homeworkReady||!homeworkRows.find(r=>r.id===n.value)?.selectable);
}
function format(seconds){const n=Math.max(0,Math.floor(seconds||0));return `${Math.floor(n/60).toString().padStart(2,"0")}:${(n%60).toString().padStart(2,"0")}`;}
function renderJob(){
  $("idle").hidden=!!current;$("job").hidden=!current;$("platform-check").hidden=current?.actionRequired!=="platform_verification";
  if(!current){$("job-status").textContent="待开始";buttons();return;}
  const j=current,scan=j.kind==="catalog",chapterMode=j.kind==="learning",work=j.kind==="homework",workScan=j.kind==="homework_catalog";
  const percent=work?(j.selectedHomeworkCount?Math.floor(100*j.processedHomework/j.selectedHomeworkCount):0):workScan?(j.status==="completed"?100:0):scan?(j.totalChapters?Math.floor(100*j.scannedChapters/j.totalChapters):0):chapterMode?(j.selectedChapterCount?Math.floor(100*j.processedChapters/j.selectedChapterCount):0):(j.duration?Math.min(100,Math.floor(100*j.localPosition/j.duration)):0);
  $("job-status").textContent=(scan||workScan)&&j.status==="completed"?(workScan?"作业列表已就绪":"目录已就绪"):labels[j.status]||j.status;
  $("job-course").textContent=j.currentCourse||"准备课程";$("job-chapter").textContent=j.currentChapter||"课程章节目录";
  $("job-video").textContent=scan?"仅读取目录，未读取章节任务":j.currentVideo||"";
  $("selection-summary").textContent=chapterMode?`已处理 ${j.processedChapters} / ${j.selectedChapterCount} 章 · 当前视频 ${format(j.localPosition)} / ${format(j.duration)}`:j.selectedTaskCount?`本次选择 ${j.selectedTaskCount} 个视频任务`:"";
  $("position").textContent=scan?`${j.scannedChapters} / ${j.totalChapters} 个章节`:chapterMode?`${j.processedChapters} / ${j.selectedChapterCount} 个章节`:`${format(j.localPosition)} / ${format(j.duration)}`;
  $("percent").textContent=percent+"%";$("progress").value=percent;
  $("passed").textContent=scan?"—":j.passedVideos+j.alreadyPassedVideos;$("discovered").textContent=scan?"—":j.videoCount;$("reports").textContent=scan?"—":j.reportsWithValidResponse;
  $("job-message").textContent=j.message;$("unsupported").textContent=j.unsupportedTasks;
  $("server-passed").textContent=scan?"未发送进度请求":j.serverIsPassed===true?"当前视频已确认完成":j.serverIsPassed===false?"当前视频尚未确认完成":"待确认";
  $("quiz-submitted").textContent=j.submittedQuizzes||0;$("quiz-passed").textContent=(j.passedQuizzes||0)+(j.alreadyPassedQuizzes||0);$("quiz-unconfirmed").textContent=j.unconfirmedQuizzes||0;
  $("learning-detail").hidden=work||workScan;$("homework-detail").hidden=!work;
  $("passed-label").textContent=work||workScan?"本次接受提交的作业":"服务器已确认视频";$("discovered-label").textContent=work||workScan?"已处理作业":"已处理视频";$("reports-label").textContent=work||workScan?"原已提交的作业":"当前视频有效响应";
  if(work||workScan){$("position").textContent=work?`${j.processedHomework} / ${j.selectedHomeworkCount} 份作业`:j.status==="completed"?`已读取 ${j.homeworkCount||0} 份作业`:"正在读取作业标题";$("job-chapter").textContent=j.currentChapter||"课程作业";$("job-video").textContent=work?j.currentVideo||"":"仅获取列表，不打开作业详情";$("selection-summary").textContent=work?`按列表顺序处理 ${j.selectedHomeworkCount} 份作业`:"";$("passed").textContent=j.submittedHomework||0;$("discovered").textContent=j.processedHomework||0;$("reports").textContent=j.alreadySubmittedHomework||0;}
  $("homework-confirmed").textContent=j.confirmedHomework||0;$("homework-bank-hits").textContent=j.bankHits||0;$("homework-generated").textContent=j.deepseekQuestions||0;$("homework-short").textContent=j.shortAnswersGenerated||0;$("homework-failed").textContent=j.failedHomework||0;$("homework-unconfirmed").textContent=j.unconfirmedHomework||0;
  $("quiz-bank-hits").textContent=j.bankHits||0;$("quiz-generated").textContent=j.deepseekQuestions||0;$("quiz-short").textContent=j.shortAnswersGenerated||0;
  $("chapter-queue").replaceChildren();
  for(const chapter of (work?j.homeworkQueue:j.chapterQueue)||[]){const li=document.createElement("li"),name=document.createElement("span"),state=document.createElement("span");name.textContent=chapter.name;state.textContent=labels[chapter.status]||chapter.status;li.append(name,state);$("chapter-queue").append(li);}
  $("notes").replaceChildren();for(const text of j.notes){const li=document.createElement("li");li.textContent=text;$("notes").append(li);}buttons();
}
function schedule(){clearTimeout(timer);timer=setTimeout(poll,current?.kind==="catalog"?400:1000);}
async function poll(){
  if(!current||terminal.has(current.status))return;const id=current.id,version=generation;
  try{
    const next=await api(`/api/jobs/${id}`);if(current?.id!==id||version!==generation)return;current=next;renderJob();
    if(next.kind==="catalog"&&terminal.has(next.status)){
      if(next.status==="completed")renderCatalog(await api(`/api/courses/${next.courses[0].id}/tasks`));
      else $("chapters-hint").textContent="目录读取未完成，请重新读取。";
    }
    if(next.kind==="homework_catalog"&&terminal.has(next.status)){
      if(next.status==="completed")renderHomework(await api(`/api/courses/${next.courses[0].id}/homework`));
      else $("homework-hint").textContent="作业列表读取未完成，请刷新作业。";
    }
    if(next.kind==="homework"&&terminal.has(next.status)){
      renderHomework(await api(`/api/courses/${next.courses[0].id}/homework`));await loadHomeworkSettings();
    }
  }catch(error){notice(error.message);}
  if(current&&!terminal.has(current.status))schedule();
}
$("login-form").addEventListener("submit",async event=>{
  event.preventDefault();$("login").disabled=true;notice();let cookie=$("cookie").value;$("cookie").value="";
  try{await cancelQr();const pending=api("/api/login",{cookie});cookie="";await connected(await pending);}catch(error){notice(error.message);}finally{$("login").disabled=false;}
});
$("qr-tab").addEventListener("click",()=>setLoginMode("qr"));$("cookie-tab").addEventListener("click",()=>setLoginMode("cookie"));
for(const mode of ["qr","cookie"]){$(mode+"-tab").addEventListener("keydown",async event=>{if(event.key!=="ArrowLeft"&&event.key!=="ArrowRight")return;event.preventDefault();const next=mode==="qr"?"cookie":"qr";await setLoginMode(next);$(next+"-tab").focus();});}
$("qr-refresh").addEventListener("click",async()=>{if(loginTransition)return;loginTransition=true;$("qr-refresh").disabled=true;try{await cancelQr();await beginQr();}finally{loginTransition=false;$("qr-refresh").disabled=false;}});
$("logout").addEventListener("click",async()=>{try{await api("/api/logout",{});disconnected();notice("已退出登录，运行中的任务将停止。");}catch(error){notice(error.message);}});
$("back").addEventListener("click",()=>{history.pushState(null,"",location.pathname);showCourseList();});
window.addEventListener("popstate",()=>{if(!csrf)return;const id=requestedCourse();if(!id)showCourseList();else if(id===selectedCourse){$("course-list-page").hidden=true;$("course-page").hidden=false;}else if(!busy&&(!current||terminal.has(current.status)))chooseCourse(id);});
$("select-all").addEventListener("change",()=>{selections.set(selectedCourse,new Set($("select-all").checked?incompleteChapters().slice(0,1000).map(ch=>ch.id):[]));renderChapters();});
$("rescan").addEventListener("click",()=>chooseCourse(selectedCourse,true));
$("refresh").addEventListener("click",async()=>{busy=true;buttons();notice();try{courses=(await api("/api/courses")).courses;selectedCourse="";chapters=[];ready=false;selections.clear();renderCourses();}catch(error){notice(error.message);}finally{busy=false;buttons();}});
$("start").addEventListener("click",async()=>{
  busy=true;buttons();notice();try{if($("deepseek-key").value.trim())await saveDeepseek();current=await api("/api/jobs",{courseIds:[selectedCourse],chapterIds:selected(),quizPolicy:$("quiz-submit").checked?($("quiz-random").checked?"random_submit":"bank_submit"):"skip"});renderJob();schedule();}catch(error){notice(error.message);}finally{busy=false;buttons();}
});
$("quiz-submit").addEventListener("change",buttons);
$("stop").addEventListener("click",async()=>{busy=true;buttons();try{await api(`/api/jobs/${current.id}/stop`,{});notice("已请求停止，当前请求返回后不再处理后续任务。");schedule();}catch(error){notice(error.message);}finally{busy=false;buttons();}});
function setCoursePanels(){
  const work=courseMode==="homework";$("chapter-mode").hidden=work;$("homework-mode").hidden=!work;$("rescan").hidden=work;
  $("chapters-tab").setAttribute("aria-selected",String(!work));$("homework-tab").setAttribute("aria-selected",String(work));
  $("course-kind").textContent=work?"03 / HOMEWORK":"03 / CHAPTERS";$("progress-note").textContent=work?"进入列表只读取标题。启动后逐份读取详情、获取答案并提交；接受提交与列表确认完成分别统计。":"目录页不读取任务。启动后仅处理所选章节，视频按实际时间推进；测验提交成功与任务通过分别核对。";
}
function selectedHomework(){return [...(homeworkSelections.get(selectedCourse)||[])].filter(id=>homeworkRows.some(r=>r.id===id&&r.selectable));}
function renderHomework(data){
  if(data.courseId!==selectedCourse)return;homeworkRows=data.homework||[];homeworkReady=data.ready===true;$("homework-list").replaceChildren();$("homework-picker").hidden=!homeworkReady;
  $("homework-count").textContent=homeworkRows.length;$("homework-hint").textContent=!homeworkReady?"正在读取作业标题…":homeworkRows.length?"状态来自服务器列表，点击刷新作业可更新。全选仅选择已确认未完成且可提交的作业；状态未知可手动选择。":"该课程当前没有独立作业；章节测验仍在章节内处理。";
  const checked=homeworkSelections.get(selectedCourse)||new Set();homeworkSelections.set(selectedCourse,checked);
  for(const work of homeworkRows){
    const row=document.createElement("label"),input=document.createElement("input"),name=document.createElement("span"),state=document.createElement("span");
    row.className="chapter";input.type="checkbox";input.value=work.id;input.checked=checked.has(work.id)&&work.selectable;
    name.className="chapter-title";name.textContent=work.name;row.title=work.selectable?work.name:"当前不可提交："+work.name;
    state.className="chapter-state "+(work.isCompleted===true?"complete":work.isCompleted===false?"incomplete":"unknown");
    state.textContent=(work.isCompleted===true?"✓ ":"")+(work.statusText||"状态未知");state.title="状态来自服务器作业列表，刷新作业可更新。";
    input.addEventListener("change",()=>{input.checked?checked.add(work.id):checked.delete(work.id);buttons();});row.append(input,name,state);$("homework-list").append(row);
  }buttons();
}
async function loadHomeworkSettings(){
  homeworkSettings=await api("/api/ai/settings");$("deepseek-status").textContent=homeworkSettings.configured?"通用 SK 已加密保存，章节测验和作业共用":"尚未配置 SK，题库未命中或有简答题时需要填写";$("bank-count").textContent=homeworkSettings.bankQuestions;
}
async function loadHomework(force=false,restoring=false){
  const version=generation,id=selectedCourse;busy=true;homeworkReady=false;homeworkRows=[];renderHomework({courseId:id,ready:false,homework:[]});buttons();notice();
  try{await loadHomeworkSettings();if(version!==generation||id!==selectedCourse)return;
    const cached=force?null:await api(`/api/courses/${id}/homework`);if(version!==generation)return;
    if(cached?.ready)renderHomework(cached);
    else if(!restoring||!current||terminal.has(current.status)){current=await api(`/api/courses/${id}/homework/scan`,{});renderJob();schedule();}
    if(restoring&&current?.kind==="homework"&&current.courses[0]?.id===id){homeworkSelections.set(id,new Set(current.selectedHomeworkIds||[]));if(cached?.ready)renderHomework(cached);}
  }catch(error){notice(error.message);$("homework-hint").textContent="作业列表读取失败，请刷新作业。";}finally{if(version===generation){busy=false;buttons();}}
}
for(const mode of ["chapters","homework"]){$(mode+"-tab").addEventListener("click",()=>{if(busy||(current&&!terminal.has(current.status)))return;history.replaceState(null,"",`#course/${encodeURIComponent(selectedCourse)}${mode==="homework"?"/homework":""}`);chooseCourse(selectedCourse);});}
$("homework-refresh").addEventListener("click",()=>loadHomework(true));
$("homework-all").addEventListener("change",()=>{homeworkSelections.set(selectedCourse,new Set($("homework-all").checked?incompleteHomework().map(r=>r.id):[]));renderHomework({courseId:selectedCourse,ready:homeworkReady,homework:homeworkRows});});
async function saveDeepseek(clear=false){const key=clear?"":$("deepseek-key").value.trim();if(!clear&&!key)throw new Error("请填写 SK；删除已有 SK 请点击清除。");$("deepseek-key").value="";await api("/api/ai/settings",{key});await loadHomeworkSettings();}
for(const [id,clear] of [["deepseek-save",false],["deepseek-clear",true]]){$(id).addEventListener("click",async()=>{busy=true;buttons();try{await saveDeepseek(clear);notice(clear?"已清除 SK。":"SK 已加密保存。");}catch(error){notice(error.message);}finally{busy=false;buttons();}});}
$("homework-start").addEventListener("click",async()=>{busy=true;buttons();notice();try{if($("deepseek-key").value.trim())await saveDeepseek();current=await api("/api/homework/jobs",{courseId:selectedCourse,workIds:selectedHomework()});renderJob();schedule();}catch(error){notice(error.message);}finally{busy=false;buttons();}});
function downloadJson(value,name){const url=URL.createObjectURL(new Blob([JSON.stringify(value,null,2)],{type:"application/json"})),a=document.createElement("a");a.href=url;a.download=name;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);}
$("bank-example").addEventListener("click",()=>downloadJson({questions:[{type:"0",text:"示例：1 加 1 等于多少？",options:[{label:"A",text:"2"},{label:"B",text:"3"}],answer:"A"} ]},"question-bank-example.json"));
$("bank-export").addEventListener("click",async()=>{try{downloadJson(await api("/api/homework/bank"),"question-bank.json");}catch(error){notice(error.message);}});
$("bank-file").addEventListener("change",async()=>{const file=$("bank-file").files[0];if(!file)return;busy=true;buttons();try{if(file.size>1900000)throw new Error("题库文件不能超过 1.9 MB");const value=JSON.parse(await file.text());const result=await api("/api/homework/bank/import",value);await loadHomeworkSettings();notice(`已导入 ${result.imported} 道选择题。`);}catch(error){notice(error instanceof SyntaxError?"题库文件不是有效 JSON":error.message);}finally{$("bank-file").value="";busy=false;buttons();}});
(async()=>{try{await connected(await api("/api/session"));}catch(error){if(error.message!=="请先登录")notice(error.message);if(!csrf&&loginMode==="qr")await beginQr();}})();
