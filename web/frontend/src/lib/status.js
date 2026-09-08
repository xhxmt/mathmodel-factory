export function statusLabel(status, displayStatus = '') {
  return {
    running: '运行中',
    paused: '已暂停',
    completed: '已完成',
    awaiting_consultation: '等待咨询',
    awaiting_selection: '等待选方案',
    ready: '就绪',
    setup: '初始化',
    failed: '失败',
    killed: '已终止',
    retrying: '重试中',
    archiving: '归档中',
    interrupted: '已中断',
    unknown: '状态待确认',
  }[status] || displayStatus || '状态待确认'
}
