import { createMemoryHistory, createRouter, createWebHashHistory } from 'vue-router'

const RouteSink = { template: '<span style="display:none" aria-hidden="true"></span>' }

export function createDashboardRouter(history = null) {
  return createRouter({
    history: history || (typeof location === 'undefined' ? createMemoryHistory() : createWebHashHistory()),
    routes: [
      { path: '/', name: 'dashboard', component: RouteSink },
      { path: '/p/:baseName', name: 'project', component: RouteSink, props: true },
      { path: '/:pathMatch(.*)*', redirect: '/' },
    ],
  })
}

export const router = createDashboardRouter()
