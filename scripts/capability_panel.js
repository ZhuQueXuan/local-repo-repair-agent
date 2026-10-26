// Creation opt-in only. Older runs retain their original tool surface.
function capabilityChoices(){
  const enabled=$('mode').value==='live'&&['dual_review','document_review'].includes($('collaborationMode').value);
  $('allowCapabilities').disabled=!enabled;
  if(!enabled)$('allowCapabilities').checked=false;
}
const priorCapabilityChoices=choices;
choices=function(){priorCapabilityChoices();capabilityChoices()};
$('collaborationMode').addEventListener('change',capabilityChoices);
$('mode').addEventListener('change',capabilityChoices);
const priorCapabilitySelect=select;
select=function(id){$('capabilityRunState').hidden=true;priorCapabilitySelect(id)};
Object.assign(eventNames,{registered_tool_receipt:'只读工具调用记录'});
const priorCapabilityDisplay=display;
display=function(row){priorCapabilityDisplay(row);$('capabilityRunState').hidden=row.harness_capabilities_version!==1;
  text($('capabilityRunState'),'本任务已开启只读工具与Skills扩展；调用记录见各阶段 tool-registry.json。MCP概览仅用于定位，不算测试或缺陷证据。')};
capabilityChoices();
