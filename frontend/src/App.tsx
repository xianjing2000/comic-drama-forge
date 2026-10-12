import React, { useState, useEffect, useMemo } from 'react';
import { AppProvider } from '@/context/AppContext';
import { ThemeProvider } from '@/context/ThemeContext';
import { ToastProvider } from '@/components/ui/toast';
import { Navbar } from '@/components/layout/Navbar';
import { Sidebar } from '@/components/layout/Sidebar';
import { ErrorBoundary } from '@/components/ErrorBoundary';
import { ServiceMonitor } from '@/components/ServiceMonitor';
import { ProjectsPage } from '@/pages/ProjectsPage';
import { AIVaultPage } from '@/pages/AIVaultPage';
import { MemoryPage } from '@/pages/MemoryPage';
import { ProjectWorkbenchPage } from '@/pages/ProjectWorkbenchPage';
import { ComfyUIPage } from '@/pages/ComfyUIPage';
import { LogsPage } from '@/pages/LogsPage';

// 全局（与具体项目无关）的功能放这里；单个项目的功能一律进 ProjectWorkbenchPage 的标签页。
// 「上传小说」已并入「项目中心 → 新建项目」，不再单独占一个入口。
const SECTIONS: Record<string, React.ComponentType<any>> = {
  projects: ProjectsPage,
  ai: AIVaultPage,
  memory: MemoryPage,
  comfyui: ComfyUIPage,
  logs: LogsPage,
};

// Parse URL hash to determine active section and optional project key
function parseRoute(): { section: string; projectKey: string | null } {
  const hash = window.location.hash;
  let path = hash.replace('#', '') || '/';
  // Normalize: #/#/foo → #/foo
  if (path.startsWith('/#')) {
    path = path.slice(1);
  }
  const params = new URLSearchParams(path.split('?')[1] || '');
  const projectKey = params.get('p');
  const route = path.split('?')[0].replace(/^\//, '');
  // If ?p= is present, render project workbench; otherwise use the section name
  const section = projectKey ? 'project-workbench' : (route && SECTIONS[route] ? route : 'projects');
  return { section, projectKey };
}

function AppContent() {
  const [activeSection, setActiveSection] = useState(() => parseRoute().section);
  const [projectKey, setProjectKey] = useState<string | null>(() => parseRoute().projectKey);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);

  // Listen for hash changes (browser back/forward, direct URL)
  useEffect(() => {
    const handleHashChange = () => {
      const { section, projectKey: pk } = parseRoute();
      setActiveSection(section);
      setProjectKey(pk);
    };
    window.addEventListener('hashchange', handleHashChange);
    return () => window.removeEventListener('hashchange', handleHashChange);
  }, []);

  // 兜底必须落在真实存在的页面上：parseRoute 只会产出 SECTIONS 的 key 或 'project-workbench'
  // （后者必带 projectKey → 走上方的 ProjectWorkbenchPage 分支），所以这里永远不会命中。
  // 曾兜底到 OverviewPage —— 那个页面因此**运行期不可达**（t25 N1），已删除；
  // 兜底改为项目中心，语义与「未知路由回默认页」一致。
  const PageComponent = useMemo(() => SECTIONS[activeSection] || ProjectsPage, [activeSection]);

  return (
    // bg-canvas 半透明：透出 body::before 的科技感辉光层（见 index.css），玻璃面板才有景深
    <div className="flex h-screen bg-canvas/75 text-ink-1">
      {/* Sidebar */}
      <Sidebar
        activeSection={activeSection}
        onNavigate={(id) => {
          setActiveSection(id);
          setProjectKey(null);
          window.location.hash = `#${id}`;
        }}
        collapsed={sidebarCollapsed}
        onToggle={() => setSidebarCollapsed(!sidebarCollapsed)}
      />

      {/* Main content */}
      <div className="flex-1 flex overflow-hidden">
        <div className="flex-1 flex flex-col overflow-hidden">
          <Navbar />
          {/* 超宽屏下限宽居中，避免一行文字拉到 2000px+ 难以阅读 */}
          <main className="w-full flex-1 overflow-y-auto p-6">
            {activeSection === 'project-workbench' && projectKey ? (
              // 审计 P2-36（2026-09-29）：key={projectKey} 强制切项目时整页重挂 ——
              // 旧实现组件实例复用，OverviewTab/QcTab/StoryboardTab 等保留上一个
              // 项目的选中集与数据，快速切换时还会出现旧项目响应覆盖新项目的竞态。
              <ProjectWorkbenchPage key={projectKey} projectKey={projectKey} />
            ) : (
              <PageComponent />
            )}
          </main>
        </div>
      </div>
    </div>
  );
}

export default function App() {
  return (
    <ErrorBoundary>
      {/* ThemeProvider 放最外层（不依赖其他 context）：深色实现 = 给 <html> 注入
          .dark 类，配合 index.css 的 :root.dark token 覆盖层整站换肤，
          组件层不感知主题、无 dark: 变体。 */}
      <ThemeProvider>
        {/* ToastProvider 必须在 AppProvider 之外：AppContext.showError 要往 Toast 里推消息 */}
        <ToastProvider>
          <AppProvider>
            <AppContent />
            <ServiceMonitor />
          </AppProvider>
        </ToastProvider>
      </ThemeProvider>
    </ErrorBoundary>
  );
}
