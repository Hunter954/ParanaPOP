import dotenv from 'dotenv';

dotenv.config();

function intEnv(name, fallback) {
  const value = Number.parseInt(process.env[name] || '', 10);
  return Number.isFinite(value) ? value : fallback;
}

export const config = {
  port: intEnv('PORT', 3001),
  serviceToken: process.env.SERVICE_TOKEN || '',
  corsOrigins: (process.env.CORS_ORIGINS || '*')
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean),
  authDir: process.env.AUTH_DIR || './auth',
  botName: process.env.BOT_NAME || 'ParanaPOP Bot',
  mediaDownloadTimeoutMs: intEnv('MEDIA_DOWNLOAD_TIMEOUT_MS', 30000),
  maxMediaBytes: intEnv('MAX_MEDIA_BYTES', 15_000_000),
  maxVideoBytes: intEnv('MAX_VIDEO_BYTES', 80_000_000),
  botPublishEnabled: ['1', 'true', 'yes', 'on', 'sim'].includes(String(process.env.BOT_PUBLISH_ENABLED || '').toLowerCase()),
  botPublishApiUrl: (process.env.BOT_PUBLISH_API_URL || '').trim(),
  botPublishToken: (process.env.BOT_PUBLISH_TOKEN || '').trim(),
  botPublishGroupId: (process.env.BOT_PUBLISH_GROUP_ID || '').trim(),
  photoParanaPopGroupId: (process.env.PHOTO_PARANAPOP_GROUP_ID || process.env.BOT_PUBLISH_GROUP_ID || '').trim(),
  photoParanaPopApiUrl: (process.env.PHOTO_PARANAPOP_API_URL || '').trim(),
  photoParanaPopToken: (process.env.PHOTO_PARANAPOP_TOKEN || process.env.BOT_PUBLISH_TOKEN || '').trim(),
  photoTrivoxGroupId: (process.env.PHOTO_TRIVOX_GROUP_ID || '').trim(),
  photoTrivoxApiUrl: (process.env.PHOTO_TRIVOX_API_URL || '').trim(),
  photoTrivoxToken: (process.env.PHOTO_TRIVOX_TOKEN || '').trim(),
  videoParanaPopApiUrl: (process.env.VIDEO_PARANAPOP_API_URL || (process.env.PHOTO_PARANAPOP_API_URL || '').replace('/generate-photo', '/generate-video')).trim(),
  videoParanaPopToken: (process.env.VIDEO_PARANAPOP_TOKEN || process.env.PHOTO_PARANAPOP_TOKEN || process.env.BOT_PUBLISH_TOKEN || '').trim(),
  videoTrivoxGroupId: (process.env.VIDEO_TRIVOX_GROUP_ID || process.env.PHOTO_TRIVOX_GROUP_ID || '').trim(),
  videoTrivoxApiUrl: (process.env.VIDEO_TRIVOX_API_URL || (process.env.PHOTO_TRIVOX_API_URL || '').replace('/generate-photo', '/generate-trivox-video')).trim(),
  videoTrivoxToken: (process.env.VIDEO_TRIVOX_TOKEN || process.env.PHOTO_TRIVOX_TOKEN || '').trim(),
  adminMenuEnabled: ['1', 'true', 'yes', 'on', 'sim'].includes(String(process.env.ADMIN_MENU_ENABLED || process.env.BOT_PUBLISH_ENABLED || '').toLowerCase()),
  adminMenuGroupId: (process.env.ADMIN_MENU_PARANAPOP_GROUP_ID || process.env.PHOTO_PARANAPOP_GROUP_ID || process.env.BOT_PUBLISH_GROUP_ID || '').trim(),
  adminMenuApiUrl: (process.env.ADMIN_MENU_PARANAPOP_API_URL || (process.env.BOT_PUBLISH_API_URL || '').replace('/api/whatsapp-bot/publish', '/api/whatsapp-menu/action')).trim(),
  adminMenuToken: (process.env.ADMIN_MENU_PARANAPOP_TOKEN || process.env.BOT_PUBLISH_TOKEN || '').trim()
};
