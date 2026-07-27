import makeWASocket, {
  DisconnectReason,
  downloadMediaMessage,
  fetchLatestBaileysVersion,
  useMultiFileAuthState
} from '@whiskeysockets/baileys';
import { Boom } from '@hapi/boom';
import P from 'pino';
import QRCode from 'qrcode';
import fs from 'fs/promises';
import path from 'path';
import axios from 'axios';
import { config } from './config.js';
import { resolveMedia } from './media.js';
import { handleAdminMenu } from './adminMenu.js';

const logger = P({ level: process.env.LOG_LEVEL || 'info' });

let sock = null;
let qrText = null;
let qrDataUrl = null;
let connected = false;
let starting = false;
let lastConnectionUpdate = null;
let lastError = null;
let reconnectTimer = null;
const publishSessions = new Map();
const photoSessions = new Map();
const videoSessions = new Map();

function botEnabled() {
  return Boolean(config.botPublishEnabled && config.botPublishApiUrl && config.botPublishToken);
}

function textFromMessage(message) {
  const m = message?.message || {};
  return (
    m.conversation ||
    m.extendedTextMessage?.text ||
    m.imageMessage?.caption ||
    m.videoMessage?.caption ||
    ''
  ).trim();
}

function imageMessageContent(message) {
  return message?.message?.imageMessage || null;
}

function videoMessageContent(message) {
  return message?.message?.videoMessage || null;
}

function sessionKey(remoteJid, participant) {
  return `${remoteJid || 'chat'}:${participant || 'user'}`;
}

async function replyMessage(jid, text) {
  if (!sock || !jid || !text) return;
  await sock.sendMessage(jid, { text });
}

function commandAllowed(remoteJid) {
  if (!botEnabled()) return false;
  if (config.botPublishGroupId && remoteJid !== config.botPublishGroupId) return false;
  return true;
}

async function imageToPayload(message) {
  const buffer = await downloadMediaMessage(
    message,
    'buffer',
    {},
    { logger, reuploadRequest: sock.updateMediaMessage }
  );
  const mimetype = imageMessageContent(message)?.mimetype || 'image/jpeg';
  return {
    image_base64: buffer.toString('base64'),
    image_mimetype: mimetype,
    image_filename: `whatsapp-${Date.now()}.${mimetype.includes('png') ? 'png' : mimetype.includes('webp') ? 'webp' : 'jpg'}`
  };
}

async function videoToPayload(message) {
  const buffer = await downloadMediaMessage(
    message,
    'buffer',
    {},
    { logger, reuploadRequest: sock.updateMediaMessage }
  );
  if (buffer.length > config.maxVideoBytes) {
    throw new Error(`O vídeo excede o limite de ${Math.floor(config.maxVideoBytes / 1_000_000)} MB.`);
  }
  const mimetype = videoMessageContent(message)?.mimetype || 'video/mp4';
  const extension = mimetype.includes('quicktime') ? 'mov' : mimetype.includes('webm') ? 'webm' : 'mp4';
  return {
    video_base64: buffer.toString('base64'),
    video_mimetype: mimetype,
    video_filename: `whatsapp-${Date.now()}.${extension}`
  };
}

async function publishToSite(session) {
  const response = await axios.post(
    config.botPublishApiUrl,
    {
      token: config.botPublishToken,
      title: session.title,
      content: session.content,
      ...session.image
    },
    {
      timeout: 60000,
      headers: {
        'X-Bot-Token': config.botPublishToken,
        'Content-Type': 'application/json'
      },
      maxBodyLength: config.maxMediaBytes + 2_000_000
    }
  );
  return response.data;
}

function photoBrandForGroup(remoteJid) {
  if (config.photoParanaPopGroupId && remoteJid === config.photoParanaPopGroupId) {
    return {
      key: 'paranapop',
      name: 'Paraná Pop',
      apiUrl: config.photoParanaPopApiUrl,
      token: config.photoParanaPopToken,
      needsCategory: true
    };
  }
  if (config.photoTrivoxGroupId && remoteJid === config.photoTrivoxGroupId) {
    return {
      key: 'trivox',
      name: 'Portal Trivox',
      apiUrl: config.photoTrivoxApiUrl,
      token: config.photoTrivoxToken,
      needsCategory: false
    };
  }
  return null;
}

async function requestManualPhoto(session) {
  if (!session.brand.apiUrl || !session.brand.token) {
    throw new Error(`Gerador manual do ${session.brand.name} não está configurado no Railway.`);
  }
  const response = await axios.post(
    session.brand.apiUrl,
    {
      token: session.brand.token,
      title: session.title,
      category: session.category || '',
      ...session.image
    },
    {
      timeout: 90000,
      headers: {
        'X-Bot-Token': session.brand.token,
        'Content-Type': 'application/json'
      },
      maxBodyLength: config.maxMediaBytes + 2_000_000
    }
  );
  return response.data;
}

async function sendGeneratedPhotos(jid, result, brand) {
  const images = Array.isArray(result?.images)
    ? result.images
    : (result?.image_url ? [{ url: result.image_url, label: 'Feed Instagram' }] : []);
  if (!images.length) throw new Error(result?.message || 'O gerador não devolveu nenhuma imagem.');

  for (const item of images) {
    const media = await resolveMedia(item.url || item.image_url);
    await sock.sendMessage(jid, {
      image: media.buffer,
      mimetype: media.mimetype,
      caption: `✅ ${brand.name} — ${item.label || item.size || 'arte gerada'}`
    });
  }
}

async function startParanaPopPhotoFlow(message) {
  const remoteJid = message?.key?.remoteJid;
  const participant = message?.key?.participant || remoteJid;
  const brand = photoBrandForGroup(remoteJid);

  if (!remoteJid || !brand || brand.key !== 'paranapop') {
    await replyMessage(remoteJid, '⚠️ A imagem padrão pelo menu está disponível somente no grupo do Paraná Pop.');
    return;
  }
  if (!brand.apiUrl || !brand.token) {
    await replyMessage(remoteJid, '⚠️ O gerador manual do Paraná Pop ainda não está configurado no Railway.');
    return;
  }

  const key = sessionKey(remoteJid, participant);
  photoSessions.set(key, { step: 'image', brand, startedAt: Date.now() });
  await replyMessage(remoteJid, '📸 *IMAGEM PADRÃO*\n\nParaná Pop: envie agora a foto principal da matéria.\n\nPara cancelar, digite */cancelar*.');
}

async function handlePhotoBot(message) {
  const remoteJid = message?.key?.remoteJid;
  const participant = message?.key?.participant || remoteJid;
  if (message?.key?.fromMe || !remoteJid || !remoteJid.endsWith('@g.us')) return false;

  const text = textFromMessage(message);
  const normalized = text.toLowerCase();
  const key = sessionKey(remoteJid, participant);
  const brand = photoBrandForGroup(remoteJid);

  if (normalized === '/foto') {
    if (!brand) return false;
    if (!brand.apiUrl || !brand.token) {
      await replyMessage(remoteJid, `⚠️ O comando /foto do ${brand.name} ainda não está configurado no Railway.`);
      return true;
    }
    photoSessions.set(key, { step: 'image', brand, startedAt: Date.now() });
    await replyMessage(remoteJid, `📸 Gerador manual do ${brand.name}. Envie agora a foto principal da matéria.`);
    return true;
  }

  const session = photoSessions.get(key);
  if (!session) return false;

  if (normalized === '/cancelar') {
    photoSessions.delete(key);
    await replyMessage(remoteJid, 'Geração cancelada. Digite /foto para começar novamente.');
    return true;
  }

  if (Date.now() - session.startedAt > 20 * 60 * 1000) {
    photoSessions.delete(key);
    await replyMessage(remoteJid, '⏰ Essa geração expirou. Digite /foto para começar novamente.');
    return true;
  }

  try {
    if (session.step === 'image') {
      if (!imageMessageContent(message)) {
        await replyMessage(remoteJid, 'Envie uma foto/imagem para continuar ou digite /cancelar.');
        return true;
      }
      session.image = await imageToPayload(message);
      session.step = 'title';
      photoSessions.set(key, session);
      await replyMessage(remoteJid, 'Foto recebida ✅ Agora envie o título da matéria.');
      return true;
    }

    if (session.step === 'title') {
      if (!text || text.length < 5) {
        await replyMessage(remoteJid, 'Envie um título com pelo menos 5 caracteres.');
        return true;
      }
      session.title = text.slice(0, 500);
      if (session.brand.needsCategory) {
        session.step = 'category';
        photoSessions.set(key, session);
        await replyMessage(remoteJid, 'Título salvo ✅ Agora envie a categoria da matéria.');
        return true;
      }
      session.step = 'generating';
    } else if (session.step === 'category') {
      if (!text || text.length < 2) {
        await replyMessage(remoteJid, 'Envie o nome da categoria para continuar.');
        return true;
      }
      session.category = text.slice(0, 120);
      session.step = 'generating';
    } else {
      return true;
    }

    photoSessions.set(key, session);
    await replyMessage(remoteJid, `🎨 Gerando a arte padrão do ${session.brand.name}...`);
    const result = await requestManualPhoto(session);
    if (!result?.ok) throw new Error(result?.message || 'Falha no gerador.');
    await sendGeneratedPhotos(remoteJid, result, session.brand);
    photoSessions.delete(key);
    await replyMessage(remoteJid, '✅ Geração concluída. Para criar outra, digite /foto.');
    return true;
  } catch (error) {
    logger.error({ error }, 'Erro no comando /foto');
    photoSessions.delete(key);
    await replyMessage(remoteJid, `⚠️ Não consegui gerar a arte: ${error?.response?.data?.message || error?.message || 'erro interno'}`);
    return true;
  }
}


function videoBrandForGroup(remoteJid) {
  if (config.photoParanaPopGroupId && remoteJid === config.photoParanaPopGroupId) {
    return {
      key: 'paranapop',
      name: 'Paraná Pop',
      apiUrl: config.videoParanaPopApiUrl,
      token: config.videoParanaPopToken,
      needsCategory: true,
      enabled: true
    };
  }
  if (config.videoTrivoxGroupId && remoteJid === config.videoTrivoxGroupId) {
    return {
      key: 'trivox',
      name: 'Portal Trivox',
      apiUrl: config.videoTrivoxApiUrl,
      token: config.videoTrivoxToken,
      needsCategory: false,
      enabled: Boolean(config.videoTrivoxApiUrl && config.videoTrivoxToken)
    };
  }
  return null;
}

async function requestManualVideo(session) {
  if (!session.brand.apiUrl || !session.brand.token) {
    throw new Error(`Gerador de vídeo do ${session.brand.name} ainda não está configurado.`);
  }
  const response = await axios.post(
    session.brand.apiUrl,
    {
      token: session.brand.token,
      title: session.title,
      category: session.category || '',
      ...session.video
    },
    {
      timeout: 240000,
      headers: {
        'X-Bot-Token': session.brand.token,
        'Content-Type': 'application/json'
      },
      maxBodyLength: config.maxVideoBytes * 1.5
    }
  );
  return response.data;
}

async function sendGeneratedVideos(jid, result, brand) {
  const videos = Array.isArray(result?.videos)
    ? result.videos
    : (result?.video_url ? [{ url: result.video_url, label: 'Vídeo Stories / Reels' }] : []);
  if (!videos.length) throw new Error(result?.message || 'O gerador não devolveu nenhum vídeo.');

  for (const item of videos) {
    const media = await resolveMedia(item.url || item.video_url);
    await sock.sendMessage(jid, {
      video: media.buffer,
      mimetype: item.mimetype || media.mimetype || 'video/mp4',
      caption: `✅ ${brand.name} — ${item.label || item.size || 'vídeo gerado'}`
    });
  }
}

async function startParanaPopVideoFlow(message) {
  const remoteJid = message?.key?.remoteJid;
  const participant = message?.key?.participant || remoteJid;
  const brand = videoBrandForGroup(remoteJid);
  if (!remoteJid || !brand || brand.key !== 'paranapop') {
    await replyMessage(remoteJid, '⚠️ O vídeo padrão pelo menu está disponível somente no grupo do Paraná Pop.');
    return;
  }
  if (!brand.apiUrl || !brand.token) {
    await replyMessage(remoteJid, '⚠️ O gerador de vídeo do Paraná Pop ainda não está configurado no Railway.');
    return;
  }
  const key = sessionKey(remoteJid, participant);
  videoSessions.set(key, { step: 'video', brand, startedAt: Date.now() });
  await replyMessage(remoteJid, '🎬 *VÍDEO PADRÃO*\n\nEnvie o vídeo que será usado no Reels/Stories. Depois pedirei o título e a categoria.\n\nPara cancelar, digite */cancelar*.');
}

async function handleVideoBot(message) {
  const remoteJid = message?.key?.remoteJid;
  const participant = message?.key?.participant || remoteJid;
  if (message?.key?.fromMe || !remoteJid || !remoteJid.endsWith('@g.us')) return false;

  const text = textFromMessage(message);
  const normalized = text.toLowerCase();
  const key = sessionKey(remoteJid, participant);
  const brand = videoBrandForGroup(remoteJid);

  if (normalized === '/video') {
    if (!brand) return false;
    if (brand.key === 'trivox' && !brand.enabled) {
      await replyMessage(remoteJid, '⚠️ O gerador de vídeo do Portal Trivox ainda não está configurado no Railway.');
      return true;
    }
    if (!brand.apiUrl || !brand.token) {
      await replyMessage(remoteJid, `⚠️ O gerador de vídeo do ${brand.name} ainda não está configurado no Railway.`);
      return true;
    }
    videoSessions.set(key, { step: 'video', brand, startedAt: Date.now() });
    await replyMessage(remoteJid, `🎬 Gerador de vídeo do ${brand.name}. Envie agora o vídeo original. O sistema identifica sozinho se ele é stories/vertical ou horizontal.`);
    return true;
  }

  const session = videoSessions.get(key);
  if (!session) return false;

  if (normalized === '/cancelar') {
    videoSessions.delete(key);
    await replyMessage(remoteJid, 'Geração de vídeo cancelada.');
    return true;
  }
  if (Date.now() - session.startedAt > 30 * 60 * 1000) {
    videoSessions.delete(key);
    await replyMessage(remoteJid, '⏰ Essa geração expirou. Inicie novamente pelo menu ou com /video.');
    return true;
  }

  try {
    if (session.step === 'video') {
      if (!videoMessageContent(message)) {
        await replyMessage(remoteJid, 'Envie um vídeo para continuar ou digite /cancelar.');
        return true;
      }
      session.video = await videoToPayload(message);
      session.step = 'title';
      videoSessions.set(key, session);
      await replyMessage(remoteJid, 'Vídeo recebido ✅ Agora envie o título da matéria.');
      return true;
    }
    if (session.step === 'title') {
      if (!text || text.length < 5) {
        await replyMessage(remoteJid, 'Envie um título com pelo menos 5 caracteres.');
        return true;
      }
      session.title = text.slice(0, 500);
      if (session.brand.needsCategory) {
        session.step = 'category';
        videoSessions.set(key, session);
        await replyMessage(remoteJid, 'Título salvo ✅ Agora envie a categoria da matéria.');
        return true;
      }
      session.step = 'generating';
    } else if (session.step === 'category') {
      if (!text || text.length < 2) {
        await replyMessage(remoteJid, 'Envie o nome da categoria para continuar.');
        return true;
      }
      session.category = text.slice(0, 120);
      session.step = 'generating';
    } else {
      return true;
    }

    videoSessions.set(key, session);
    await replyMessage(remoteJid, session.brand.key === 'trivox' ? `🎞️ Processando o vídeo do ${session.brand.name} no formato detectado automaticamente...` : `🎞️ Processando o vídeo vertical do ${session.brand.name}...`);
    const result = await requestManualVideo(session);
    if (!result?.ok) throw new Error(result?.message || 'Falha no gerador de vídeo.');
    await sendGeneratedVideos(remoteJid, result, session.brand);
    videoSessions.delete(key);
    await replyMessage(remoteJid, session.brand.key === 'trivox' ? '✅ Vídeo concluído no padrão do Portal Trivox.' : '✅ Vídeo concluído e pronto para Reels/Stories.');
    return true;
  } catch (error) {
    logger.error({ error }, 'Erro no comando /video');
    videoSessions.delete(key);
    await replyMessage(remoteJid, `⚠️ Não consegui gerar o vídeo: ${error?.response?.data?.message || error?.message || 'erro interno'}`);
    return true;
  }
}

async function handlePublishBot(message) {
  const remoteJid = message?.key?.remoteJid;
  const participant = message?.key?.participant || message?.key?.remoteJid;
  const fromMe = Boolean(message?.key?.fromMe);
  if (fromMe || !remoteJid || !remoteJid.endsWith('@g.us')) return;

  const text = textFromMessage(message);
  const key = sessionKey(remoteJid, participant);

  if (text.toLowerCase() === '/publicar') {
    if (!commandAllowed(remoteJid)) {
      await replyMessage(remoteJid, '🤖 Bot Publicar ainda não está configurado/ativo para este grupo. Confira o menu Admin > Bot Publicar.');
      return;
    }
    publishSessions.set(key, { step: 'image', startedAt: Date.now() });
    await replyMessage(remoteJid, '🚀 Vamos publicar uma matéria nova. Primeiro, envie a imagem principal da matéria.');
    return;
  }

  if (text.toLowerCase() === '/cancelar') {
    if (publishSessions.delete(key)) {
      await replyMessage(remoteJid, 'Publicação cancelada. Quando quiser começar de novo, digite /publicar.');
    }
    return;
  }

  const session = publishSessions.get(key);
  if (!session) return;

  if (Date.now() - session.startedAt > 30 * 60 * 1000) {
    publishSessions.delete(key);
    await replyMessage(remoteJid, '⏰ Essa publicação expirou. Digite /publicar para começar novamente.');
    return;
  }

  try {
    if (session.step === 'image') {
      if (!imageMessageContent(message)) {
        await replyMessage(remoteJid, '📸 Agora preciso da imagem da matéria. Envie uma foto/imagem ou digite /cancelar.');
        return;
      }
      session.image = await imageToPayload(message);
      session.step = 'title';
      publishSessions.set(key, session);
      await replyMessage(remoteJid, 'Imagem recebida ✅ Agora envie o título da matéria.');
      return;
    }

    if (session.step === 'title') {
      if (!text || text.length < 5) {
        await replyMessage(remoteJid, 'Me envie um título um pouco maior para a matéria.');
        return;
      }
      session.title = text.slice(0, 500);
      session.step = 'content';
      publishSessions.set(key, session);
      await replyMessage(remoteJid, 'Título salvo ✅ Agora envie o texto completo da matéria.');
      return;
    }

    if (session.step === 'content') {
      if (!text || text.length < 20) {
        await replyMessage(remoteJid, 'O texto parece curto demais. Envie o conteúdo completo da matéria ou /cancelar.');
        return;
      }
      session.content = text;
      session.step = 'publishing';
      publishSessions.set(key, session);
      await replyMessage(remoteJid, '🛠️ Recebi tudo. Estou publicando a matéria agora...');
      const result = await publishToSite(session);
      publishSessions.delete(key);
      if (result?.ok) {
        await replyMessage(remoteJid, `✅ Matéria publicada com sucesso!\n\n${result.title || session.title}\n${result.url || ''}`.trim());
      } else {
        await replyMessage(remoteJid, `⚠️ Não consegui publicar: ${result?.message || 'erro desconhecido'}`);
      }
    }
  } catch (error) {
    logger.error({ error }, 'Erro no Bot Publicar');
    publishSessions.delete(key);
    await replyMessage(remoteJid, `⚠️ Deu erro ao publicar: ${error?.response?.data?.message || error?.message || 'erro interno'}`);
  }
}


async function setQr(qr) {
  qrText = qr || null;
  qrDataUrl = qr ? await QRCode.toDataURL(qr, { margin: 1, width: 320 }) : null;
}

function scheduleReconnect() {
  if (reconnectTimer) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    startWhatsApp().catch((error) => {
      lastError = error.message;
      logger.error({ error }, 'Erro ao reconectar WhatsApp');
      scheduleReconnect();
    });
  }, 3000);
}

export async function startWhatsApp() {
  if (starting) return;
  starting = true;

  try {
    await fs.mkdir(config.authDir, { recursive: true });
    const { state, saveCreds } = await useMultiFileAuthState(config.authDir);
    const { version } = await fetchLatestBaileysVersion();

    sock = makeWASocket({
      version,
      logger,
      printQRInTerminal: false,
      auth: state,
      browser: [config.botName, 'Chrome', '1.0.0'],
      syncFullHistory: false,
      markOnlineOnConnect: false
    });

    sock.ev.on('creds.update', saveCreds);

    sock.ev.on('messages.upsert', async ({ messages }) => {
      for (const message of messages || []) {
        const remoteJid = message?.key?.remoteJid;
        const participant = message?.key?.participant || remoteJid;
        const text = textFromMessage(message);
        const menuHandled = await handleAdminMenu({
          message,
          config,
          text,
          remoteJid,
          participant,
          reply: (replyText) => replyMessage(remoteJid, replyText),
          startPhotoFlow: () => startParanaPopPhotoFlow(message),
          startVideoFlow: () => startParanaPopVideoFlow(message)
        });
        if (menuHandled) continue;
        const videoHandled = await handleVideoBot(message);
        if (videoHandled) continue;
        const handled = await handlePhotoBot(message);
        if (!handled) await handlePublishBot(message);
      }
    });

    sock.ev.on('connection.update', async (update) => {
      lastConnectionUpdate = update;
      const { connection, lastDisconnect, qr } = update;

      if (qr) {
        connected = false;
        await setQr(qr);
        logger.info('Novo QR Code disponível');
      }

      if (connection === 'open') {
        connected = true;
        lastError = null;
        await setQr(null);
        logger.info('WhatsApp conectado');
      }

      if (connection === 'close') {
        connected = false;
        await setQr(null);

        const statusCode = new Boom(lastDisconnect?.error)?.output?.statusCode;
        lastError = lastDisconnect?.error?.message || `Conexão fechada (${statusCode || 'sem código'})`;

        logger.warn({ statusCode, error: lastError }, 'WhatsApp desconectado');

        if (statusCode !== DisconnectReason.loggedOut) {
          scheduleReconnect();
        } else {
          logger.warn('Sessão deslogada. Será necessário escanear novo QR Code.');
        }
      }
    });
  } finally {
    starting = false;
  }
}

export function getStatus() {
  return {
    ok: true,
    status: connected ? 'Conectado' : (qrText ? 'Aguardando QR Code' : 'Desconectado'),
    state: connected ? 'connected' : (qrText ? 'qr' : 'disconnected'),
    connected,
    has_qr: Boolean(qrText),
    qr: qrText,
    qr_data_url: qrDataUrl,
    phone: sock?.user?.id || null,
    user: sock?.user || null,
    last_error: lastError,
    last_update: lastConnectionUpdate
      ? {
          connection: lastConnectionUpdate.connection || null,
          received_pending_notifications: lastConnectionUpdate.receivedPendingNotifications || null
        }
      : null
  };
}

export function getQr() {
  return {
    ok: true,
    connected,
    has_qr: Boolean(qrText),
    qr: qrText,
    qr_data_url: qrDataUrl
  };
}

export async function logoutWhatsApp() {
  if (sock) {
    try {
      await sock.logout();
    } catch (error) {
      logger.warn({ error }, 'Falha ao fazer logout pelo socket');
    }
  }
  connected = false;
  await setQr(null);
  try {
    await fs.rm(config.authDir, { recursive: true, force: true });
  } catch (error) {
    logger.warn({ error }, 'Falha ao remover diretório de auth');
  }
  scheduleReconnect();
  return { ok: true };
}

function ensureConnected() {
  if (!sock || !connected) {
    throw new Error('WhatsApp ainda não está conectado. Escaneie o QR Code no admin.');
  }
}

export async function listGroups() {
  ensureConnected();
  const chats = await sock.groupFetchAllParticipating();
  const groups = Object.values(chats)
    .map((group) => ({
      id: group.id,
      name: group.subject,
      participants: group.participants?.length || 0,
      participants_count: group.participants?.length || 0,
      owner: group.owner || null,
      announce: Boolean(group.announce),
      restrict: Boolean(group.restrict)
    }))
    .sort((a, b) => a.name.localeCompare(b.name, 'pt-BR'));

  return { ok: true, groups };
}

export async function sendMessage({ group_id, message }) {
  ensureConnected();
  if (!group_id) throw new Error('group_id ausente.');
  if (!message) throw new Error('message ausente.');

  const result = await sock.sendMessage(group_id, { text: message });
  return { ok: true, message_id: result?.key?.id || null };
}

export async function sendImage({ group_id, image_url, image_path, caption }) {
  ensureConnected();
  if (!group_id) throw new Error('group_id ausente.');

  const media = await resolveMedia(image_url || image_path);
  const result = await sock.sendMessage(group_id, {
    image: media.buffer,
    mimetype: media.mimetype,
    caption: caption || ''
  });

  return { ok: true, message_id: result?.key?.id || null };
}

function pickUrl(item) {
  return item?.url || item?.image_url || item?.public_url || item?.path || item?.image_path || null;
}

function labelForFormat(format) {
  const normalized = String(format || '').toLowerCase();
  if (normalized.includes('stories') || normalized.includes('story')) return 'Stories';
  if (normalized.includes('facebook')) return 'Facebook';
  if (normalized.includes('feed')) return 'Feed';
  return format || 'Arte';
}

function firstFilled(...values) {
  for (const value of values) {
    if (value !== undefined && value !== null && String(value).trim() !== '') {
      return String(value).trim();
    }
  }
  return '';
}

function boolValue(value, fallback = false) {
  if (value === undefined || value === null || value === '') return fallback;
  if (typeof value === 'boolean') return value;
  return ['1', 'true', 'yes', 'on', 'sim'].includes(String(value).toLowerCase());
}

function buildNewsCaption({ payload, siteName, title, summary, url, instagramDescription }) {
  const prefix = firstFilled(
    payload.message_prefix,
    payload.prefix,
    `📰 Nova matéria publicada no ${siteName}`
  );

  const parts = [prefix, '', title];

  if (summary && boolValue(payload.include_summary, false)) {
    parts.push('', summary);
  }

  if (instagramDescription && instagramDescription !== title) {
    const cleanDescription = instagramDescription.trim();
    const duplicatedPrefix = cleanDescription.includes(prefix) || cleanDescription.includes(title);
    if (!duplicatedPrefix) {
      parts.push('', cleanDescription);
    }
  }

  if (url && boolValue(payload.include_link, true)) {
    parts.push('', `Leia agora: ${url}`);
  }

  return parts.filter((part) => part !== undefined && part !== null).join('\n').trim();
}

export async function sendNews(payload = {}) {
  ensureConnected();

  const groupId = payload.group_id || payload.groupId || payload.to;
  if (!groupId) throw new Error('group_id ausente.');

  const post = payload.post || {};
  const siteName = firstFilled(payload.portal_name, payload.site_name, payload.brand_name, post.portal_name, post.site_name, 'Paraná Pop');
  const title = firstFilled(payload.title, payload.titulo, post.title, 'Nova matéria');
  const url = firstFilled(payload.url, payload.post_url, post.url);
  const summary = firstFilled(payload.summary, payload.resumo, post.summary);
  const instagramDescription = firstFilled(
    payload.instagram_description,
    payload.description,
    payload.descricao_instagram,
    payload.caption
  );
  const images = Array.isArray(payload.images) ? payload.images : [];
  const caption = firstFilled(
    payload.whatsapp_caption,
    payload.image_caption,
    buildNewsCaption({ payload, siteName, title, summary, url, instagramDescription })
  );

  const sent = [];

  for (const [index, image] of images.entries()) {
    const mediaUrl = pickUrl(image);
    if (!mediaUrl) continue;

    const formatLabel = labelForFormat(image.format || image.type || image.name);
    const imageCaption = index === 0 ? caption : firstFilled(image.caption, `Arte ${formatLabel} - ${title}`);
    const result = await sendImage({
      group_id: groupId,
      image_url: /^https?:\/\//i.test(mediaUrl) ? mediaUrl : undefined,
      image_path: /^https?:\/\//i.test(mediaUrl) ? undefined : mediaUrl,
      caption: imageCaption
    });

    sent.push({ format: formatLabel, ...result });
  }

  const shouldSendText = boolValue(payload.send_text, images.length === 0);
  let textResult = null;

  if (shouldSendText) {
    textResult = await sendMessage({
      group_id: groupId,
      message: caption
    });
  }

  return {
    ok: true,
    site_name: siteName,
    sent_images: sent,
    sent_text: textResult
  };
}
