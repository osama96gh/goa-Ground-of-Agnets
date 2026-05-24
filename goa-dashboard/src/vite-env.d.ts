/// <reference types="vite/client" />

interface ImportMetaEnv {
  /**
   * Workspace admin token, inlined by vite.config.ts in dev mode so the
   * dashboard auto-fills the AdminTokenGate. Empty string in production
   * builds — see vite.config.ts.
   */
  readonly VITE_GOA_ADMIN_TOKEN: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
