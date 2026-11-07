# PowerShell examples for the installed sau CLI.

# account_name is user-defined. One account_name maps to one account file.
# Xiaohongshu always launches a visible local Google Chrome window; --headless is rejected.
$account = "account_a"
$video = "videos/demo.mp4"
$thumbnail = "videos/demo.png"
$noteImages = @("videos/1.png", "videos/2.png")

sau xiaohongshu login --account $account
sau xiaohongshu check --account $account

sau xiaohongshu upload-video `
  --account $account `
  --file $video `
  --title "Xiaohongshu video from PowerShell" `
  --desc "Xiaohongshu video description from PowerShell" `
  --tags "cli,video" `
  --thumbnail $thumbnail

sau xiaohongshu upload-note `
  --account $account `
  --images $noteImages `
  --title "Xiaohongshu note title from PowerShell" `
  --note "Xiaohongshu note from PowerShell" `
  --tags "cli,note"
