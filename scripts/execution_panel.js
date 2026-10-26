// Explicit host inspection; never starts a task, model or package installation.
let executionInspectBusy=false;
const runtimeLabels={available:'版本检查可用',not_probed:'路径已定位，尚未执行检查',missing:'未找到',unavailable:'检查不可用',probe_failed:'版本检查失败',unsupported_version:'版本不满足声明'};
function showExecutionReport(value){
  const box=$('executionEnvironmentResult');box.replaceChildren();
  box.append(node('p','当前面板进程的本机环境；每次新任务另存自己的环境声明。','hint'));
  for(const [name,item] of Object.entries(value.runtimes)){
    const row=node('div');row.append(node('strong',name.toUpperCase()+' · '+(runtimeLabels[item.status]||item.status)),
      node('pre',(item.executable||'无路径')+'\n'+(item.version||'版本未确认')+(item.reason?'\n原因：'+item.reason:'')));
    box.append(row);
  }
  box.append(node('p','Node版本可用不等于任务服务已支持JavaScript。当前自动任务/评分仍按已登记Python任务；此检查不安装依赖、不请求模型，当前后端没有OS沙箱。','hint'));
}
$('inspectExecutionEnvironment').onclick=async()=>{
  if(executionInspectBusy)return;executionInspectBusy=true;$('inspectExecutionEnvironment').disabled=true;
  text($('executionEnvironmentNotice'),'正在运行固定版本命令；不会创建任务或调用模型…');
  try{const value=await api('/api/execution-environment/inspect',{});showExecutionReport(value);
    text($('executionEnvironmentNotice'),'检查已完成 · '+new Date(value.recorded_at).toLocaleString('zh-CN',{hour12:false}));
  }catch(error){text($('executionEnvironmentNotice'),error.message)}
  finally{executionInspectBusy=false;$('inspectExecutionEnvironment').disabled=false}
};
