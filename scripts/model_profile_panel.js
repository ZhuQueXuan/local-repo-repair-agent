// Capability declarations only; loading this catalog never probes model accounts.
let modelProfileCatalog=null;
function modelProfileChoices(){
  $('modelProfile').disabled=$('mode').value!=='live'||!modelProfileCatalog;
}
const previousModelChoices=choices;
choices=function(){previousModelChoices();modelProfileChoices()};
$('mode').addEventListener('change',modelProfileChoices);
const previousModelDisplay=display;
display=function(row){previousModelDisplay(row);
  text($('modelProfileRunState'),row.model_profile_id?'模型配置：'+row.model_profile_id+'\n角色绑定：'+JSON.stringify(row.model_profile_snapshot?.role_bindings||{})+'\n配置哈希：'+row.model_profile_sha256:
    row.kind==='replay'?'离线补丁重放没有模型配置。':'未显式选择模型配置的历史/默认记录；不补造当时能力或真实验证结果。')};
function modelProfileCreation(){
  if($('mode').value!=='live')return undefined;
  if(!modelProfileCatalog)throw Error('模型能力目录尚未加载，请刷新后创建任务。');
  const item=modelProfileCatalog.profiles.find(p=>p.id===$('modelProfile').value);
  if(!item||item.status!=='enabled')throw Error('所选候选仅离线格式适配，当前不能联网运行。');
  return item.id;
}
(async()=>{
  modelProfileCatalog=await api('/api/model-profiles');
  const box=$('modelProfile');box.replaceChildren();
  for(const p of modelProfileCatalog.profiles){const item=node('option',p.label);item.value=p.id;item.disabled=p.status!=='enabled';box.append(item)}
  box.value=modelProfileCatalog.default_profile;
  text($('modelProfileDetails'),'原路由保持，全部角色共享一条路由；官方分支关闭思考，网关思考能力未声明。沿用原上下文/输出保护和费用记录。Claude候选仅具备离线文本/工具格式适配，没有联网传输或账号验证。');
  modelProfileChoices();
})().catch(error=>{text($('modelProfileDetails'),'模型配置目录不可用：'+error.message);modelProfileChoices()});
