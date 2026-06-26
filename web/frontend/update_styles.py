import re

def update_file(filepath, new_styles):
    with open(filepath, 'r') as f:
        content = f.read()
    
    # Replace everything between <style scoped> and </style>
    pattern = re.compile(r'<style scoped>.*?</style>', re.DOTALL)
    new_content = pattern.sub('<style scoped>\n' + new_styles + '\n</style>', content)
    
    with open(filepath, 'w') as f:
        f.write(new_content)

new_project_modal_styles = """
.modal-overlay {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  background: rgba(0, 0, 0, 0.6);
  backdrop-filter: blur(8px);
  -webkit-backdrop-filter: blur(8px);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
  padding: 1rem;
  animation: fadeIn 0.3s ease;
}

.modal-drag-overlay {
  position: fixed;
  top: 1.5rem;
  left: 1.5rem;
  right: 1.5rem;
  bottom: 1.5rem;
  background: rgba(16, 18, 30, 0.85);
  backdrop-filter: blur(12px);
  z-index: 1001;
  display: flex;
  align-items: center;
  justify-content: center;
  border: 3px dashed var(--color-primary);
  border-radius: var(--radius-xl);
  box-shadow: 0 0 40px rgba(59, 130, 246, 0.2);
  transition: var(--transition-normal);
}

.modal-drag-overlay .drag-content {
  text-align: center;
  color: var(--color-text-main);
  pointer-events: none;
}

.modal-drag-overlay .drag-icon {
  font-size: 5rem;
  margin-bottom: 1.5rem;
  display: inline-block;
  animation: float 3s ease-in-out infinite;
}

.modal-drag-overlay h2 {
  font-size: 2rem;
  font-weight: 700;
  margin-bottom: 0.5rem;
  color: var(--color-primary);
  text-shadow: 0 0 10px var(--color-primary-glow);
}

.modal-drag-overlay p {
  color: var(--color-text-muted);
  font-size: 1.125rem;
  font-weight: 500;
}

@keyframes float {
  0%, 100% { transform: translateY(0); }
  50% { transform: translateY(-15px); }
}

@keyframes fadeIn {
  from { opacity: 0; }
  to { opacity: 1; }
}

.modal-container {
  background: var(--color-bg-surface);
  backdrop-filter: var(--color-bg-blur);
  -webkit-backdrop-filter: var(--color-bg-blur);
  border: 1px solid var(--border-color);
  border-radius: var(--radius-xl);
  width: 100%;
  max-width: 640px;
  max-height: 90vh;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  box-shadow: var(--shadow-lg);
  animation: slideUp 0.4s cubic-bezier(0.16, 1, 0.3, 1);
}

@keyframes slideUp {
  from { opacity: 0; transform: translateY(20px) scale(0.98); }
  to { opacity: 1; transform: translateY(0) scale(1); }
}

.modal-header {
  padding: 1.5rem 2rem;
  border-bottom: 1px solid var(--border-color);
  display: flex;
  justify-content: space-between;
  align-items: center;
  background: rgba(255, 255, 255, 0.02);
}

.modal-header h3 {
  font-size: 1.25rem;
  font-weight: 700;
  color: var(--color-text-main);
  margin: 0;
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.close-button {
  background: rgba(255, 255, 255, 0.05);
  border: 1px solid transparent;
  color: var(--color-text-muted);
  font-size: 1.25rem;
  cursor: pointer;
  padding: 0;
  width: 2.25rem;
  height: 2.25rem;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: var(--radius-md);
  transition: var(--transition-normal);
}

.close-button:hover {
  background: rgba(239, 68, 68, 0.1);
  color: #ef4444;
  border-color: rgba(239, 68, 68, 0.2);
  transform: rotate(90deg);
}

.modal-body {
  padding: 2.5rem 2rem;
  overflow-y: auto;
}

.form-group {
  margin-bottom: 1.75rem;
}

.form-group:last-child {
  margin-bottom: 0;
}

.form-group label {
  display: block;
  font-size: 0.875rem;
  font-weight: 600;
  color: var(--color-text-main);
  margin-bottom: 0.75rem;
  letter-spacing: 0.02em;
}

.required {
  color: var(--color-danger);
  margin-left: 0.25rem;
}

.form-group input[type="text"] {
  width: 100%;
  padding: 0.875rem 1rem;
  background: rgba(0, 0, 0, 0.2);
  border: 1px solid var(--border-color);
  border-radius: var(--radius-md);
  color: var(--color-text-main);
  font-size: 0.95rem;
  transition: var(--transition-normal);
  box-sizing: border-box;
  font-family: 'Fira Code', 'Monaco', monospace;
  box-shadow: inset 0 2px 4px rgba(0,0,0,0.1);
}

.form-group input[type="text"]:focus {
  outline: none;
  border-color: var(--color-primary);
  background: rgba(0, 0, 0, 0.4);
  box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.2), inset 0 2px 4px rgba(0,0,0,0.1);
}

.form-group input[type="text"]:disabled {
  opacity: 0.5;
  cursor: not-allowed;
  background: rgba(255, 255, 255, 0.02);
}

.form-group input[type="text"]::placeholder {
  color: var(--color-text-muted);
  opacity: 0.7;
}

.upload-tabs {
  display: flex;
  gap: 0.75rem;
  margin-bottom: 1.25rem;
  background: rgba(0,0,0,0.2);
  padding: 0.375rem;
  border-radius: var(--radius-md);
  border: 1px solid var(--border-color);
}

.upload-tab {
  flex: 1;
  padding: 0.625rem 1rem;
  background: transparent;
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
  color: var(--color-text-muted);
  font-size: 0.875rem;
  font-weight: 600;
  cursor: pointer;
  transition: var(--transition-normal);
}

.upload-tab:hover:not(:disabled) {
  color: var(--color-text-main);
  background: rgba(255,255,255,0.05);
}

.upload-tab.active {
  background: var(--color-bg-surface);
  border-color: var(--border-color);
  color: var(--color-primary);
  box-shadow: var(--shadow-sm);
}

.upload-tab:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.upload-area {
  margin-top: 0.5rem;
  animation: fadeIn 0.3s ease;
}

.upload-dropzone {
  border: 2px dashed rgba(255, 255, 255, 0.2);
  border-radius: var(--radius-lg);
  padding: 3rem 1.5rem;
  text-align: center;
  cursor: pointer;
  transition: var(--transition-normal);
  background: rgba(0, 0, 0, 0.2);
}

.upload-dropzone:hover {
  border-color: rgba(59, 130, 246, 0.5);
  background: rgba(59, 130, 246, 0.05);
}

.upload-dropzone.drag-over {
  border-color: var(--color-primary);
  background: rgba(59, 130, 246, 0.1);
  transform: scale(1.02);
  box-shadow: 0 0 20px rgba(59, 130, 246, 0.15);
}

.upload-icon {
  font-size: 3rem;
  margin-bottom: 1rem;
  opacity: 0.8;
  transition: transform 0.3s ease;
}

.upload-dropzone:hover .upload-icon {
  transform: translateY(-5px);
  opacity: 1;
}

.upload-text {
  font-size: 1.05rem;
  color: var(--color-text-main);
  font-weight: 600;
  margin: 0 0 0.5rem 0;
}

.upload-hint {
  font-size: 0.8rem;
  color: var(--color-text-muted);
  margin: 0;
}

.uploaded-file {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 1.25rem;
  background: rgba(59, 130, 246, 0.08);
  border: 1px solid rgba(59, 130, 246, 0.3);
  border-radius: var(--radius-md);
  box-shadow: inset 0 0 15px rgba(59, 130, 246, 0.05);
}

.file-info {
  display: flex;
  align-items: center;
  gap: 1.25rem;
  flex: 1;
  min-width: 0;
}

.file-icon {
  font-size: 2.25rem;
  flex-shrink: 0;
}

.file-details {
  flex: 1;
  min-width: 0;
}

.file-name {
  font-size: 0.95rem;
  color: var(--color-text-main);
  font-weight: 600;
  margin: 0 0 0.25rem 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.file-size {
  font-size: 0.8rem;
  color: var(--color-text-muted);
  margin: 0;
  font-family: 'Fira Code', 'Monaco', monospace;
}

.remove-file-button {
  background: rgba(239, 68, 68, 0.15);
  border: 1px solid transparent;
  color: #fca5a5;
  font-size: 1.25rem;
  width: 2.5rem;
  height: 2.5rem;
  border-radius: var(--radius-sm);
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: var(--transition-normal);
  flex-shrink: 0;
}

.remove-file-button:hover:not(:disabled) {
  background: rgba(239, 68, 68, 0.25);
  border-color: rgba(239, 68, 68, 0.4);
  color: #fff;
}

.remove-file-button:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.upload-progress {
  margin-top: 1.25rem;
}

.progress-bar {
  height: 0.5rem;
  background: rgba(0, 0, 0, 0.3);
  border-radius: 0.25rem;
  overflow: hidden;
  margin-bottom: 0.5rem;
  box-shadow: inset 0 1px 2px rgba(0,0,0,0.2);
}

.progress-fill {
  height: 100%;
  background: var(--color-primary-gradient);
  transition: width 0.3s ease;
  border-radius: 0.25rem;
  box-shadow: 0 0 10px var(--color-primary-glow);
}

.progress-text {
  font-size: 0.8rem;
  color: var(--color-text-muted);
  text-align: right;
  margin: 0;
  font-family: 'Fira Code', 'Monaco', monospace;
  font-weight: 600;
}

.hint {
  font-size: 0.8rem;
  color: var(--color-text-muted);
  margin: 0.5rem 0 0 0;
}

.checkbox-label {
  display: flex;
  align-items: flex-start;
  gap: 0.75rem;
  cursor: pointer;
  font-size: 0.9rem;
  color: var(--color-text-main);
  font-weight: 500;
  padding: 0.5rem 0;
  transition: var(--transition-fast);
}

.checkbox-label:hover {
  color: #fff;
}

.checkbox-label input[type="checkbox"] {
  width: 1.25rem;
  height: 1.25rem;
  cursor: pointer;
  margin-top: 0.125rem;
  accent-color: var(--color-primary);
}

.checkbox-label input[type="checkbox"]:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.error-message {
  padding: 1rem 1.25rem;
  background: rgba(239, 68, 68, 0.1);
  border: 1px solid rgba(239, 68, 68, 0.3);
  border-radius: var(--radius-md);
  color: #fca5a5;
  font-size: 0.875rem;
  margin-top: 1.5rem;
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.error-message::before {
  content: '⚠️';
}

.modal-footer {
  display: flex;
  gap: 1rem;
  justify-content: flex-end;
  padding: 1.5rem 2rem;
  border-top: 1px solid var(--border-color);
  background: rgba(255, 255, 255, 0.01);
}

.button {
  padding: 0.75rem 1.75rem;
  border: 1px solid transparent;
  border-radius: var(--radius-md);
  font-size: 0.95rem;
  font-weight: 600;
  cursor: pointer;
  transition: var(--transition-normal);
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.button:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.button-secondary {
  background: rgba(255, 255, 255, 0.05);
  color: var(--color-text-main);
  border: 1px solid var(--border-color);
}

.button-secondary:hover:not(:disabled) {
  background: rgba(255, 255, 255, 0.1);
  border-color: rgba(255, 255, 255, 0.2);
}

.button-primary {
  background: var(--color-primary-gradient);
  color: white;
  box-shadow: 0 4px 12px var(--color-primary-glow);
}

.button-primary:hover:not(:disabled) {
  transform: translateY(-2px);
  box-shadow: 0 6px 16px var(--color-primary-glow);
  background: linear-gradient(135deg, #3b82f6 0%, #2563eb 100%);
}

.button-primary:active:not(:disabled) {
  transform: translateY(0);
}

.button-spinner {
  width: 16px;
  height: 16px;
  border: 2px solid rgba(255, 255, 255, 0.3);
  border-top-color: white;
  border-radius: 50%;
  animation: spin 0.8s cubic-bezier(0.5, 0.1, 0.5, 0.9) infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}
"""

login_form_styles = """
.login-overlay {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  background: var(--color-bg-base);
  background-image: 
    radial-gradient(circle at 15% 50%, rgba(59, 130, 246, 0.15), transparent 35%),
    radial-gradient(circle at 85% 30%, rgba(16, 185, 129, 0.15), transparent 35%);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 9999;
}

.login-container {
  width: 100%;
  max-width: 440px;
  padding: 2rem;
  animation: slideUp 0.6s cubic-bezier(0.16, 1, 0.3, 1);
}

@keyframes slideUp {
  from { opacity: 0; transform: translateY(40px); }
  to { opacity: 1; transform: translateY(0); }
}

.login-card {
  background: var(--color-bg-surface);
  backdrop-filter: var(--color-bg-blur);
  -webkit-backdrop-filter: var(--color-bg-blur);
  border: 1px solid var(--border-color);
  border-radius: var(--radius-xl);
  padding: 3rem 2.5rem;
  box-shadow: var(--shadow-lg), 0 0 40px rgba(59, 130, 246, 0.05);
}

.login-header {
  text-align: center;
  margin-bottom: 2.5rem;
}

.login-icon {
  font-size: 3.5rem;
  display: block;
  margin-bottom: 1rem;
  animation: float 4s ease-in-out infinite;
}

@keyframes float {
  0%, 100% { transform: translateY(0); }
  50% { transform: translateY(-10px); }
}

.login-header h2 {
  font-size: 2rem;
  font-weight: 800;
  color: var(--color-text-main);
  margin: 0 0 0.5rem 0;
  background: linear-gradient(to right, #fff, #a5b4fc);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
}

.login-subtitle {
  font-size: 0.95rem;
  color: var(--color-text-muted);
  margin: 0;
  letter-spacing: 0.05em;
}

.login-form {
  margin-bottom: 2rem;
}

.form-group {
  margin-bottom: 1.5rem;
}

.form-group label {
  display: block;
  font-size: 0.875rem;
  font-weight: 600;
  color: var(--color-text-main);
  margin-bottom: 0.5rem;
}

.form-group input {
  width: 100%;
  padding: 0.875rem 1rem;
  background: rgba(0, 0, 0, 0.2);
  border: 1px solid var(--border-color);
  border-radius: var(--radius-md);
  color: var(--color-text-main);
  font-size: 1rem;
  transition: var(--transition-normal);
  box-sizing: border-box;
  box-shadow: inset 0 2px 4px rgba(0,0,0,0.1);
}

.form-group input:focus {
  outline: none;
  border-color: var(--color-primary);
  background: rgba(0, 0, 0, 0.4);
  box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.2), inset 0 2px 4px rgba(0,0,0,0.1);
}

.form-group input:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.form-group input::placeholder {
  color: var(--color-text-muted);
  opacity: 0.7;
}

.error-message {
  padding: 0.875rem 1rem;
  background: rgba(239, 68, 68, 0.1);
  border: 1px solid rgba(239, 68, 68, 0.3);
  border-radius: var(--radius-md);
  color: #fca5a5;
  font-size: 0.875rem;
  margin-bottom: 1.5rem;
  display: flex;
  align-items: center;
  gap: 0.5rem;
  animation: shake 0.5s cubic-bezier(.36,.07,.19,.97) both;
}

.error-message::before {
  content: '⚠️';
}

@keyframes shake {
  10%, 90% { transform: translate3d(-1px, 0, 0); }
  20%, 80% { transform: translate3d(2px, 0, 0); }
  30%, 50%, 70% { transform: translate3d(-4px, 0, 0); }
  40%, 60% { transform: translate3d(4px, 0, 0); }
}

.login-button {
  width: 100%;
  padding: 1rem;
  background: var(--color-primary-gradient);
  border: none;
  border-radius: var(--radius-md);
  color: white;
  font-size: 1.05rem;
  font-weight: 700;
  cursor: pointer;
  transition: var(--transition-normal);
  display: flex;
  align-items: center;
  justify-content: center;
  box-shadow: 0 4px 15px var(--color-primary-glow);
  letter-spacing: 0.05em;
}

.login-button:hover:not(:disabled) {
  transform: translateY(-2px);
  box-shadow: 0 8px 25px var(--color-primary-glow);
  background: linear-gradient(135deg, #3b82f6 0%, #2563eb 100%);
}

.login-button:active:not(:disabled) {
  transform: translateY(0);
}

.login-button:disabled {
  opacity: 0.6;
  cursor: not-allowed;
  box-shadow: none;
}

.button-spinner {
  width: 18px;
  height: 18px;
  border: 2px solid rgba(255, 255, 255, 0.3);
  border-top-color: white;
  border-radius: 50%;
  animation: spin 0.8s cubic-bezier(0.5, 0.1, 0.5, 0.9) infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

.login-footer {
  text-align: center;
  padding-top: 1.5rem;
  border-top: 1px solid var(--border-color);
}

.hint {
  font-size: 0.8rem;
  color: var(--color-text-muted);
  margin: 0.375rem 0;
}

@media (max-width: 480px) {
  .login-container {
    padding: 1rem;
  }

  .login-card {
    padding: 2rem 1.5rem;
  }
}
"""

update_file('src/components/NewProjectModal.vue', new_project_modal_styles)
update_file('src/components/LoginForm.vue', login_form_styles)

print("Styles updated successfully.")
