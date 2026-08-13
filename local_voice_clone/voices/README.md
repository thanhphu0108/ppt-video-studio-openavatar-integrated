# Voice profiles

Mỗi profile chỉ được sử dụng khi bạn có quyền dùng giọng nói đó.

Profile mặc định cần hai file local:

```text
voices/default/reference.wav
voices/default/transcript.txt
```

`reference.wav` có thể là WAV, MP3 hoặc M4A khi upload qua API/UI. Với profile
đăng ký sẵn, nên dùng WAV sạch, dài 3–12 giây và `transcript.txt` phải đúng lời
được nói trong file mẫu. Không đưa file audio mẫu lên Git.

