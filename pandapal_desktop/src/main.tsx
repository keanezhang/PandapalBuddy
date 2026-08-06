import "./monaco-setup";
import "monaco-inline-diff-review/styles";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import { BackendProvider } from "./providers/BackendProvider";
import { ErrorBoundary, reportLastCrash } from "./components/ErrorBoundary";

// 上一次会话若有渲染崩溃，先把现场打到 console（见 ErrorBoundary）
reportLastCrash();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <BrowserRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
    <ErrorBoundary>
      <BackendProvider>
        <App />
      </BackendProvider>
    </ErrorBoundary>
  </BrowserRouter>
);
