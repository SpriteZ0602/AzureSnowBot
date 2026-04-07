import { defineStore } from 'pinia'
import { ref } from 'vue'
import api from '../api'

export const useAuthStore = defineStore('auth', () => {
  const username = ref('')
  const isLoggedIn = ref(!!localStorage.getItem('access_token'))

  async function login(user: string, password: string) {
    const { data } = await api.post('/auth/login', { username: user, password })
    localStorage.setItem('access_token', data.access_token)
    localStorage.setItem('refresh_token', data.refresh_token)
    username.value = user
    isLoggedIn.value = true
  }

  function logout() {
    localStorage.removeItem('access_token')
    localStorage.removeItem('refresh_token')
    username.value = ''
    isLoggedIn.value = false
  }

  return { username, isLoggedIn, login, logout }
})
