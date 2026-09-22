<template>
  <div class="login-mask">
    <div class="login-box">
      <h2 class="login-title">{{ initialized ? '登录' : '设置管理员密码' }}</h2>
      <p class="login-sub">
        {{ initialized ? '请输入管理员密码以访问网关' : '首次访问，请设置管理员密码（将加密保存）' }}
      </p>
      <input
        class="login-input"
        type="password"
        v-model="password"
        :placeholder="initialized ? '密码' : '新密码（至少 6 位）'"
        @keyup.enter="submit"
        autofocus
      />
      <p v-if="error" class="login-error">{{ error }}</p>
      <button class="login-btn" :disabled="loading" @click="submit">
        {{ loading ? '处理中…' : (initialized ? '登录' : '设置并进入') }}
      </button>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { login, authStatus } from '../auth.js'

const emit = defineEmits(['success'])

const password = ref('')
const error = ref('')
const loading = ref(false)
const initialized = ref(true)

onMounted(async () => {
  const st = await authStatus()
  initialized.value = st.initialized
})

async function submit() {
  const pwd = String(password.value || '')
  if (!pwd) {
    error.value = '请输入密码'
    return
  }
  if (!initialized.value && pwd.length < 6) {
    error.value = '密码至少 6 位'
    return
  }
  loading.value = true
  error.value = ''
  const r = await login(pwd)
  loading.value = false
  if (r.ok) {
    password.value = ''
    emit('success')
  } else {
    error.value = r.msg || '登录失败'
  }
}
</script>

<style scoped>
.login-mask {
  position: fixed;
  inset: 0;
  z-index: 9999;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #0f1115;
}
.login-box {
  width: 340px;
  padding: 32px 28px;
  border-radius: 12px;
  background: #1a1d24;
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.45);
}
.login-title {
  margin: 0 0 6px;
  font-size: 19px;
  font-weight: 600;
  color: #e8eaed;
}
.login-sub {
  margin: 0 0 18px;
  font-size: 13px;
  line-height: 1.5;
  color: #8b909a;
}
.login-input {
  width: 100%;
  box-sizing: border-box;
  padding: 10px 12px;
  font-size: 14px;
  color: #e8eaed;
  background: #12141a;
  border: 1px solid #2c303a;
  border-radius: 8px;
  outline: none;
}
.login-input:focus {
  border-color: #4a7cf7;
}
.login-error {
  margin: 10px 0 0;
  font-size: 13px;
  color: #e5636a;
}
.login-btn {
  width: 100%;
  margin-top: 18px;
  padding: 10px;
  font-size: 14px;
  font-weight: 500;
  color: #fff;
  background: #4a7cf7;
  border: none;
  border-radius: 8px;
  cursor: pointer;
}
.login-btn:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}
</style>
