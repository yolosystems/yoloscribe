import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import './themes/dark.css'
import './themes/light.css'
import './themes/yolo.css'
import './App.css'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
