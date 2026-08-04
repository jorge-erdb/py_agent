/**
 * Static configuration for the frontend.
 *
 * Everything is served same-origin by FastAPI (see main.py), so endpoints are
 * root-relative — there is no host to configure.
 */

export const CONFIG = {
  ENDPOINTS: {
    CHAT_STREAM: '/chat/stream',
    NEW_SESSION: '/new-session',
    SESSIONS: '/sessions',
    CLEAR: '/clear',
    DESTROY_ALL: '/destroy_all',
    HEALTH: '/health',
  },

  // localStorage key holding the active session id
  SESSION_KEY: 'agent.session_id',

  // localStorage key holding the UI preferences object
  SETTINGS_KEY: 'agent.settings',
};
