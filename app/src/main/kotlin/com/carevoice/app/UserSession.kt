package com.carevoice.app

import android.content.Context
import android.content.SharedPreferences

object UserSession {
    private const val PREFS_NAME = "user_session"
    private const val KEY_TOKEN = "access_token"
    private const val KEY_USER_ID = "user_id"
    private const val KEY_FULL_NAME = "full_name"
    private const val KEY_ROLE = "role"
    private const val KEY_ROOM_NUMBER = "room_number"
    private const val KEY_WARD = "ward"
    private const val KEY_USERNAME = "username"

    private fun getPrefs(context: Context): SharedPreferences =
        context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

    fun save(
        context: Context,
        token: String,
        userId: Int,
        fullName: String,
        role: String,
        roomNumber: String?,
        ward: String? = null,
        username: String? = null
    ) {
        getPrefs(context).edit().apply {
            putString(KEY_TOKEN, token)
            putInt(KEY_USER_ID, userId)
            putString(KEY_FULL_NAME, fullName)
            putString(KEY_ROLE, role)
            putString(KEY_ROOM_NUMBER, roomNumber)
            putString(KEY_WARD, ward)
            putString(KEY_USERNAME, username)
            apply()
        }
    }

    fun getToken(context: Context): String?      = getPrefs(context).getString(KEY_TOKEN, null)
    fun getUserId(context: Context): Int         = getPrefs(context).getInt(KEY_USER_ID, -1)
    fun getFullName(context: Context): String?   = getPrefs(context).getString(KEY_FULL_NAME, null)
    fun getRole(context: Context): String?       = getPrefs(context).getString(KEY_ROLE, null)
    fun getRoomNumber(context: Context): String? = getPrefs(context).getString(KEY_ROOM_NUMBER, null)
    fun getWard(context: Context): String?       = getPrefs(context).getString(KEY_WARD, null)
    fun getUsername(context: Context): String?   = getPrefs(context).getString(KEY_USERNAME, null)

    fun isLoggedIn(context: Context): Boolean = getToken(context) != null

    fun logout(context: Context) {
        getPrefs(context).edit().clear().apply()
    }
}
