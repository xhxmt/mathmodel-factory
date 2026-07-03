export function statusLabel(status) {
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
  }[status] || status
}
