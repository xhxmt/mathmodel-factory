<template>
<section class="panel audit-status" data-testid="audit-status">
          <div>当前执行：{{ project.execution_state || project.status }} · r{{ project.revision }}</div>
          <div>工作流错误：{{ project.workflow_error || '无' }}</div>
          <div>证据：{{ project.evidence_validity || 'UNAVAILABLE' }}</div>
          <div v-for="error in project.evidence_errors" :key="error">{{ error }}</div>
          <div>科学结论：{{ project.scientific_verdict || 'UNAVAILABLE' }}<span v-if="project.review_mode === 'math_only'">（数学预审）</span></div>
          <div>诊断分数：{{ project.score_available ? project.diagnostic_score : '不可用' }} · 正式分数：{{ project.official_score ?? '不可用' }}</div>
          <div>交付：{{ project.delivery_allowed ? '允许' : '未获准' }}</div>
        </section>
</template>

<script setup>
defineProps({ project: { type: Object, required: true } })
</script>

<style scoped>
.audit-status { padding: 16px 18px; display: grid; gap: 6px; font-size: 12px; }
</style>
