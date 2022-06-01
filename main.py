import logging
import os
import random
import re
import shutil
import string
import tweepy
import requests
import youtube_dl
from telegram import Update, InputMediaPhoto
from telegram.error import TimedOut, NetworkError, BadRequest
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext, PicklePersistence

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.WARNING  # INFO for job queue logs
)

logger = logging.getLogger(__name__)

# Twitter API authentication
CONSUMER_KEY = ''
CONSUMER_SECRET = ''
auth = tweepy.AppAuthHandler(CONSUMER_KEY, CONSUMER_SECRET)

# Create Twitter API object
api = tweepy.API(auth, wait_on_rate_limit=True, wait_on_rate_limit_notify=True)

# Telegram users which may interact with the bot
AUTHORIZED_USERS = ['']
# Telegram bot token
BOT_TOKEN = ''
# Fetch new posts at this interval
DELAY_MINUTES = 5

def authorized(update: Update):
    return update.message.from_user['username'] in AUTHORIZED_USERS


# Start or resume the repeating job
def cmd_start(update: Update, context: CallbackContext) -> None:
    if not authorized(update):
        update.message.reply_text('Paste a link to a Twitter post to turn it into a Telegram post.')
        return

    init_user_data(context)
    if len(context.user_data['accounts'].keys()) == 0:
        update.message.reply_text('Use /help for a list of commands.')
    else:
        update.message.reply_text('Resuming...')

    user_id = update.message.from_user['id']
    current_jobs = context.job_queue.get_jobs_by_name(str(user_id))
    if not current_jobs:
        context.job_queue.run_repeating(fetch_tweets, interval=60*DELAY_MINUTES, first=1,
                                        context=[update, context], name=str(user_id))


# Stop the repeating job
def cmd_stop(update: Update, context: CallbackContext) -> None:
    if not authorized(update): return
    user_id = update.message.from_user['id']
    current_jobs = context.job_queue.get_jobs_by_name(str(user_id))
    if current_jobs:
        for job in current_jobs:
            job.schedule_removal()
            update.message.reply_text('Stopped fetching tweets')


# Send a message when the command /help is issued
def cmd_help(update: Update, context: CallbackContext) -> None:
    if not authorized(update): return
    update.message.reply_text('/start - resume fetching tweets, necessary after bot restart\n' +
                              '/stop - pause fetching tweets\n' +
                              '/help - list of commands\n' +
                              '/follow account_handle - follow Twitter account\n' +
                              '/unfollow account_handle - unfollow Twitter account\n' +
                              '/list - list all followed Twitter accounts\n' +
                              '/replies [on/off] - include replies, default is on\n' +
                              '/caption - reply to a media post with this to remove the caption\n\n\n'
                              'Send a tweet link to turn it into a Telegram post.\n')


# Setting to include tweet replies
def cmd_replies(update: Update, context: CallbackContext) -> None:
    if not authorized(update): return
    init_user_data(context)
    try:
        replies = update.message.text.split(' ')[1]
        if replies == 'on':
            context.user_data['replies'] = True
        elif replies == 'off':
            context.user_data['replies'] = False
        else:
            raise Exception()
    except:
        update.message.reply_text('Incorrect input')
        return


# Follow Twitter account
def cmd_follow(update: Update, context: CallbackContext) -> None:
    if not authorized(update): return
    init_user_data(context)
    try:
        account = update.message.text.split(' ')[1].replace('@', '')
    except:
        update.message.reply_text('Incorrect input')
        return

    if account in context.user_data['accounts'].keys():
        update.message.reply_text('Already following '' + account + ''')
    else:
        try:
            # id of most recent tweet
            id = get_last_tweet(account)
            context.user_data['accounts'][account] = id
            update.message.reply_text('Followed @' + account)
        except:
            update.message.reply_text('Unable to follow @' + account)


# Unfollow Twitter account
def cmd_unfollow(update: Update, context: CallbackContext) -> None:
    if not authorized(update): return
    init_user_data(context)
    try:
        account = update.message.text.split(' ')[1].replace('@', '')
    except:
        update.message.reply_text('Incorrect input')
        return

    if account in context.user_data['accounts'].keys():
        context.user_data['accounts'].pop(account, None)
        update.message.reply_text('Unfollowed @' + account)
    else:
        update.message.reply_text('Not following @' + account)


# List followed Twitter accounts
def cmd_list(update: Update, context: CallbackContext) -> None:
    if not authorized(update): return
    init_user_data(context)
    if len(context.user_data['accounts']) == 0:
        update.message.reply_text('You are not following any accounts.')
    else:
        accounts = []
        for account in context.user_data['accounts']:
            accounts.append('@' + account)
        update.message.reply_text(', '.join(accounts))


# Remove caption
def cmd_caption(update: Update, context: CallbackContext) -> None:
    if not authorized(update): return
    reply_to = update.message.reply_to_message
    if reply_to is not None:
        try:
            context.bot.edit_message_caption(chat_id=update.message.from_user['id'],
                                             message_id=reply_to.message_id, caption='')
        except BadRequest:
            return


# Fetch and post a tweet
def cmd_get_tweet(update: Update, context: CallbackContext) -> None:
    if not authorized(update): return
    url = update.message.text
    try:
        try:
            id = id_from_url(url)
        except ValueError:
            logger.error('Invalid URL: ' + update.message.text)
            return
        status = get_tweet(id)
        post_tweet(update, context, status)
    except Exception as e:
        logger.error('Failed to post tweet: ' + update.message.text + '\n' + str(e))


# Processes tweet and posts it to the bot
def post_tweet(update: Update, context: CallbackContext, status):
    is_reply = status.in_reply_to_status_id is not None
    if is_reply:
        is_self_reply = status.in_reply_to_screen_name == status.user.screen_name
    else:
        is_self_reply = False
    is_retweet = hasattr(status, 'retweeted_status')

    header = link_to_tweet(status, is_reply, is_self_reply)

    if is_retweet:
        is_self_rt = status.user.screen_name == status.retweeted_status.user.screen_name
        status = status.retweeted_status

    message = status.full_text

    if is_reply:
        replied_status = get_tweet(status.in_reply_to_status_id)
        replied_message = replied_status.full_text
        message = remove_initial_mentions(message)

    if hasattr(status, 'quoted_status'):
        quoted_message = status.quoted_status.full_text

    if is_reply and hasattr(replied_status, 'quoted_status'):
        replied_quoted_message = replied_status.quoted_status.full_text

    # Expand URLs
    message = expand_urls(status, message)
    if is_reply:
        replied_message = expand_urls(replied_status, replied_message)
    if hasattr(status, 'quoted_status'):
        quoted_message = expand_urls(status, quoted_message)
    if is_reply and hasattr(replied_status, 'quoted_status'):
        replied_quoted_message = expand_urls(replied_status.quoted_status, replied_quoted_message)

    # Remove t.co links
    message = re.sub(r'https://t.co/\w{10}', '', message)
    if is_reply:
        replied_message = re.sub(r'https://t.co/\w{10}', '', replied_message)
        if hasattr(replied_status, 'quoted_status'):
            replied_quoted_message = re.sub(r'https://t.co/\w{10}', '', replied_quoted_message)
    if hasattr(status, 'quoted_status'):
        quoted_message = re.sub(r'https://t.co/\w{10}', '', quoted_message)

    # If it's a quote tweet, remove the link to the quoted tweet
    if hasattr(status, 'quoted_status'):
        quote_url = status.quoted_status_permalink['expanded']
        message = message.replace(quote_url, '')
    if is_reply and hasattr(replied_status, 'quoted_status'):
        quote_url = replied_status.quoted_status_permalink['expanded']
        replied_message = replied_message.replace(quote_url, '')

    if is_retweet:
        if is_self_rt:
            message = header + '\n' + message
        else:
            retweeted_header = link_to_tweet(status)
            message = header + '\n' + 'RT ' + retweeted_header + '\n' + message
    if hasattr(status, 'quoted_status'):
        quoted_header = link_to_tweet(status.quoted_status)
        message = message.strip()
        if len(message) != 0:
            message = message + '\n\n'
        message = '\n' + message + 'RT ' + quoted_header + '\n' + quoted_message
        if not is_retweet:
            message = header + message
    if not is_retweet and not hasattr(status, 'quoted_status'):
        message = header + '\n' + message

    if is_reply:
        replied_message = replied_message.strip()
        if len(replied_message) != 0:
            replied_message = replied_message + '\n\n'
        if hasattr(replied_status, 'quoted_status'):
            replied_quoted_message = replied_quoted_message.strip()
            if len(replied_quoted_message) != 0:
                replied_quoted_message = replied_quoted_message + '\n\n'
            else:
                replied_quoted_message = replied_quoted_message + '\n'
            replied_quoted_header = link_to_tweet(replied_status.quoted_status)
            message = link_to_tweet(replied_status) + '\n' + replied_message + 'RT ' + replied_quoted_header + '\n' + replied_quoted_message + message
        else:
            message = link_to_tweet(replied_status) + '\n' + replied_message + message

    # Check if there is media embedded
    image_urls = []
    video_url = ''
    if has_media(status):
        image_urls = images(status)
        video_url = video(status)

    if hasattr(status, 'quoted_status'):
        if has_media(status.quoted_status) and not has_media(status):
            image_urls = images(status.quoted_status)
            video_url = video(status.quoted_status)

    if is_reply and len(image_urls) == 0 and len(video_url) == 0:
        if has_media(replied_status):
            image_urls = images(replied_status)
            video_url = video(replied_status)
        elif hasattr(replied_status, 'quoted_status') and has_media(replied_status.quoted_status):
            image_urls = images(replied_status.quoted_status)
            video_url = video(replied_status.quoted_status)

    if len(image_urls) > 0:
        # Tweet contains one or more images
        filenames = save_images(image_urls)
        try:
            if len(image_urls) == 1:
                send_image_post(update, context, message, filenames[0])
            else:
                # More than one image
                try:
                    send_gallery_post(update, context, message, filenames)
                except BadRequest as e:
                    failed_url = 'https://twitter.com/' + status.user.screen_name + '/status/' + str(status.id)
                    logger.error('BadRequest: ' + failed_url)
        except TimedOut:
            logger.error('TimedOut: https://twitter.com/' + status.user.screen_name + '/status/' + str(status.id))
        for filename in filenames:
            os.remove(filename)

    if len(video_url) > 0:
        # Tweet contains a video
        tmp_msg = context.bot.send_message(chat_id=update.message.from_user['id'], text='Downloading video ...')
        filename = save_video(video_url, str(status.id))
        if os.path.exists(filename + '.mp4'):
            try:
                post_msg = None
                try:
                    post_msg = send_video_post(update, context, message, filename + '.mp4')
                except NetworkError:
                    # try again
                    logger.error('NetworkError, trying again: ' + filename)
                    if post_msg is not None:
                        send_video_post(update, context, message, filename + '.mp4')
            except TimedOut:
                logger.error('TimedOut: https://twitter.com/' + status.user.screen_name + '/status/' + str(status.id))
            os.remove(filename + '.mp4')
            context.bot.delete_message(chat_id=update.message.from_user['id'], message_id=tmp_msg.message_id)
        else:
            logger.error('File extension error: ' + filename)

    if len(image_urls) == 0 and len(video_url) == 0:
        # Only text in the post
        try:
            send_text_post(update, context, message)
        except TimedOut:
            logger.error('TimedOut: https://twitter.com/' + status.user.screen_name + '/status/' + str(status.id))


# Removes mentions that clutter up threads
def remove_initial_mentions(text):
    regex_mentions = '^(@([A-Za-z0-9-_]+[A-Za-z0-9-_]+)\s)+'
    replaced = re.sub(regex_mentions, '', text)
    return replaced.lstrip()


# Main function
def main():
    pp = PicklePersistence(filename='db')
    updater = Updater(BOT_TOKEN, use_context=True, persistence=pp)

    # Get the dispatcher to register handlers
    dispatcher = updater.dispatcher

    dispatcher.add_handler(CommandHandler('start', cmd_start))
    dispatcher.add_handler(CommandHandler('stop', cmd_stop))
    dispatcher.add_handler(CommandHandler('help', cmd_help))
    dispatcher.add_handler(CommandHandler('follow', cmd_follow))
    dispatcher.add_handler(CommandHandler('unfollow', cmd_unfollow))
    dispatcher.add_handler(CommandHandler('list', cmd_list))
    dispatcher.add_handler(CommandHandler('replies', cmd_replies))
    dispatcher.add_handler(CommandHandler('caption', cmd_caption))

    dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, cmd_get_tweet))

    # Start the bot
    updater.start_polling()
    # Ctrl-C to exit
    updater.idle()


# Fetch all new tweets for all followed accounts
def fetch_tweets(context):
    job = context.job

    tweets = {}
    for account in job.context[1].user_data['accounts']:
        since_id = job.context[1].user_data['accounts'][account]
        most_recent = since_id
        try:
            recent_tweets = get_tweets_since(account, since_id, job.context[1].user_data['replies'])
            for tweet in recent_tweets:
                tweets[tweet.id] = tweet
                if tweet.id > most_recent:
                    most_recent = tweet.id
            job.context[1].user_data['accounts'][account] = most_recent
        except tweepy.TweepError as e:
            logger.error('TweepError: ' + str(e) + ' - while fetching account: @' + account)

    for id in sorted(tweets):
        try:
            post_tweet(job.context[0], job.context[1], tweets[id])
        except tweepy.TweepError as e:
            logger.error('TweepError: ' + str(e) + ' - for tweet: https://twitter.com/' +
                         tweets[id].user.screen_name + '/status/' + str(tweets[id].id))
        except Exception as e:
            raise e


# Check if a tweet contains media
def has_media(status):
    return hasattr(status, 'extended_entities') and 'media' in status.extended_entities


# Initialize user data
def init_user_data(context):
    if 'accounts' not in context.user_data.keys():
        context.user_data['accounts'] = {}
    if 'replies' not in context.user_data.keys():
        context.user_data['replies'] = True


# Return a list of filenames of images, check if tweet contains media before calling
def images(status):
    image_urls = []
    for media in status.extended_entities.get('media', [{}]):
        if media.get('type', None) == 'photo':
            image_urls.append(media['media_url'])
    return image_urls


# Return a URL for video, or '' if no video, check if tweet contains media before calling
def video(status):
    for media in status.extended_entities.get('media', [{}]):
        if media.get('type', None) == 'video' or media.get('type', None) == 'animated_gif':
            return 'https://twitter.com/' + status.user.screen_name + '/status/' + str(status.id)
    return ''


# Expand URLs in the text
def expand_urls(status, message):
    if hasattr(status, 'retweeted_status'):
        # Expand links in retweeted message
        status = status.retweeted_status
    if 'urls' in status.entities:
        for embedded_url in status.entities.get('urls', [{}]):
            message = message.replace(embedded_url['url'], embedded_url['expanded_url'])
        return message


# Return the id in a tweet URL
def id_from_url(url):
    if '?' in url:
        url = url.split('?')[0]
    return int(url.split('/')[-1])


# Fetch the most recent tweet
def get_last_tweet(username):
    status = api.user_timeline(screen_name=username, count=1, include_rts=1, tweet_mode='extended')[0]
    return status.id


# Fetch a tweet with a particular id
def get_tweet(id):
    status = api.get_status(id, tweet_mode='extended')
    return status


# Fetch all tweets newer than a particular id
def get_tweets_since(account, id, include_replies):
    tweets = []
    exclude = not include_replies
    for status in tweepy.Cursor(api.user_timeline, screen_name=account,
                                tweet_mode='extended', since_id=id, exclude_replies=exclude).items():
        tweets.append(status)
    return tweets


# Send a post with text only to the bot
def send_text_post(update, context, message):
    context.bot.send_message(chat_id=update.message.from_user['id'], text=message,
                                 parse_mode='HTML', disable_web_page_preview=True)


# Send a post with one image to the bot
def send_image_post(update, context, message, filename):
    context.bot.send_photo(chat_id=update.message.from_user['id'], photo=open(filename, 'rb'),
                                caption=message, parse_mode='HTML')


# Send a post with multiple images to the bot
def send_gallery_post(update, context, message, filenames):
    group = []
    # Put caption on the first image or it won't show
    first_filename = filenames.pop(0)
    group.append(InputMediaPhoto(open(first_filename, 'rb'), caption=message, parse_mode='HTML'))
    for filename in filenames:
        group.append(InputMediaPhoto(open(filename, 'rb')))
    filenames.append(first_filename)
    context.bot.send_media_group(chat_id=update.message.from_user['id'], media=group)


# Send a post with a video file to the bot
def send_video_post(update, context, message, filename):
    return context.bot.send_video(chat_id=update.message.from_user['id'], video=open(filename, 'rb'),
                               caption=message, parse_mode='HTML')


# Takes a list of image URLs, downloads the images and returns the filenames
def save_images(image_urls):
    filenames = []
    for image_url in image_urls:
        filename = './media/' + ''.join(random.choices(string.ascii_uppercase + string.digits, k=15))
        filenames.append(filename)
        r = requests.get(image_url, stream=True)
        if r.status_code == 200:
            r.raw.decode_content = True
            with open(filename, 'wb') as f:
                shutil.copyfileobj(r.raw, f)
    return filenames


# Takes a video URL, downloads the video and returns the filename
def save_video(video_url, filename):
    ydl_opts = {'outtmpl': './media/' + filename + '.%(ext)s', 'quiet': True}
    with youtube_dl.YoutubeDL(ydl_opts) as ydl:
        ydl.download([video_url])
    return './media/' + filename


# Returns a link to a Twitter post, with context if needed
def link_to_tweet(status, is_reply=False, is_self_reply=False):
    url = 'https://twitter.com/' + status.user.screen_name + '/status/' + str(status.id)
    link = '<a href="' + url + '">' + status.user.name + ' (@' + status.user.screen_name + ')' + '</a>'
    if is_reply or is_self_reply:
        if is_self_reply:
            link += ' continued:'
        else:
            link += ' replied:'
    return link


if __name__ == '__main__':
    main()
