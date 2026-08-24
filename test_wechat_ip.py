import requests

corpid = "wwf35acf2abf80360b"
corpsecret = "dmiz23nCg6OtT56pnphVUk94LScSB_f3aV5r1uxhhzM"

res = requests.get(f"https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid={corpid}&corpsecret={corpsecret}")
print("Access token result:", res.json())
