#!/usr/bin/env python
from enum import Enum
import paho.mqtt.client as mqtt  #import the client1
from datetime import datetime
import time

import signal   #to detect CTRL C
import sys

import json #to generate payloads for mqtt publishing
import jsonpickle #json.dumps crashes for InstantMessage. jsonpickle works fine
import random #for the disarm pin
import threading    #for timing the countdowns
import os.path # to check if configuration file exists
import re # regular expression to detect trigger events
import requests # for translation

sys.path.append( os.path.abspath('../mqtt_notifier' ) )
from aux import *
from gsm import SMS

from pprint import pprint #for debug printing

class MqttParams( object ):
    """ Holds the mqtt connection params
    """
    def __init__( self, address, port, subscribeTopic, publishTopic ):
        self.address = address
        self.port = port
        self.subscribeTopic = subscribeTopic
        self.publishTopic = publishTopic

class StatusMain( Enum ):
    """The possible status values for the alarm status main property

    Values:
        UNARMED:    self explanatory
        ARMING:     counting down until it becomes ARMED
        ARMED_HOME: armed with people in the house/pace (usefull in order to not monitor certain events such as lights turning on)
        ARMED_AWAY: armed in an empty house/place(i.e. monitor extra events such as lights turing on, etc.)
        TRIGGERED:  an event has triggered the alarm and is counting down until it gets ACTIVATED
        ACTIVATED:  sirens screaming, lights flashing, sending sms/email/IM messages, making alert phone calls etc
    """
    UNAVAILABLE = 0
    UNARMED = 1
    ARMING = 2
    ARMED_HOME = 3
    ARMED_AWAY = 4
    TRIGGERED = 5
    ACTIVATED = 6

class Status( object ):
    """ The status of an alarm

    Attributes:
        main: the actual status eumeration
        countdown: the countdown value when ARMING  or TRIGGERED
        challengePin: The disarm message must contain the correct pin answer i.e. (challengePin + disarmPin)%10
    """
    def __init__( self, main = StatusMain.UNARMED, countdown = 0, challengePin = '' ):
        self.main = main
        self.countdown = countdown
        self.challengePin = challengePin

    def toJson( self ):
        """ Json strings must enclose tokens and string literals in double quotes
        """
        return json.dumps( { 'main': self.main.name, 'countdown': self.countdown, 'challengePin': self.challengePin } )

    def __eq__( self, other ):
        if isinstance(other, self.__class__):
            return self.__dict__ == other.__dict__
        return False

    def __ne__( self, other ):
        """Define a non-equality test
        """
        return not self.__eq__(other)

class AlarmCommand( Enum ):
    """ The commands that the alarm accepts over mqtt
    """
    ARM_HOME = 1
    ARM_AWAY = 2
    DEACTIVATE_REQUEST = 3  #this is the first step when disarming a triggered alarm. The alarm responds with a 10-base moduled pin and then the DISARM command must contain the correct answer
    DEACTIVATE = 4
    DISARM = 5


class EmailConfig( object ):
    def __init__( self, sender, recipients, subject ):
        self.sender = sender
        self.subject = subject
        self.recipients = recipients

class Notification( object ):
    def __init__( self, notifierMqttPublish, messageTemplate, translationUrl ):
        self.notifierMqttPublish = notifierMqttPublish
        self.messageTemplate = messageTemplate
        self.translationUrl = translationUrl

    def generateMessage( self, mqttMessage ):
        translation = None
        message = self.messageTemplate
        try:
            response = requests.get( self.translationUrl + mqttMessage.topic )
            translation = response.json()
            # print( 'received translation: ', json.dumps( translation, indent = 2 ).decode('utf-8') )
            device_name = ( translation['house'] + '/' + translation['floor'] + '/' + translation['room'] + '/' + translation['item'] ).encode('utf-8')
            # print( 'device_name: ', device_name.decode('utf-8') )
            # print( 'messsage: ', message )
            message = message.replace( '{device_name}', device_name.decode('utf-8') ).encode('utf-8')
            # print( 'payload: {}'.format( mqttMessage.payload.decode( "utf-8" ) ) )
            # print( 'now message: {}'.format( message ) )
            message = message.decode('utf-8').replace( '{text}', mqttMessage.payload.decode( "utf-8" ) ).encode('utf-8')
        except Exception as e:
            print( 'error: ', e.message, e.args )
            pass
        print( 'returning message: {}'.format( message ).decode('utf-8') )
        return message


class Notify( object ):
    def __init__( self, sms = [], phonecall = [], im = [], email = None ):
        self.sms = sms
        self.phonecall = phonecall
        self.im = im
        self.email = email
    
    def toMqttCommand( self, messageText ):
        print( 'messageText: {}, sms: {}, phonecall: {}, im: {}, email: {}'.format( messageText, self.sms, self.phonecall, self.im, self.email ) )
        return jsonpickle.encode( { 
            'sms': SMS( self.sms, messageText  ), 
            'phonecall': self.phonecall, 
            'im': InstantMessage( self.im, messageText ),
            'email': None if self.email is None else Email( self.email.sender,  self.email.recipients, self.email.subject, messageText )
        } )
        

class Trigger( object ):
    """ Definition of how to detect trigger events. When armed the alarm will subscribe to certain mqtt topics..
        When such a message arrives and the corresponding regex matches, the alarm will trigger
    
    Attributes:
        topics: list of topics to subscibe and listen for incoming messages
        regex: a regeular expression to be matched against the payload of the received message. If matched the alarm triggers
    """
    def __init__( self, topics, regex, notify ):
        self.topics = topics
        self.regex = regex
        self.notify = notify

class MqttPublish( object ):
    """ This class holds the configuration for a publish command to be executed when starting or stopping an arm state """
    def __init__( self, topic, command ):
         self.topic = topic
         self.command = command

class ArmConfig( object ):
    """ This class holds the configuration for an arm state such as arm_home and arm_away, as it is loaded from configuration file """
    def __init__( self, start, stop, triggers ):
        """ 
            start: the mqtt publish messages to send e.g. mqtt motion sensor activate
            stop: the mqtt publish messages to send e.g. mqtt motion sensor deactivate
            triggers: objects holding the description of what will trigger the alarm and what will happen
        """
        self.start = start
        self.stop = stop
        self.triggers = triggers

class MqttAlarm( object ):
    """ An alarm that publishes its status using mqtt and receives commands the same way
    """

    def __init__( self, armingCountdown, triggeredCountdown, disarmPin, mqttId, mqttParams, armedAway, armedHome, notification, status = Status( StatusMain.UNARMED, 0 ) ):
        self.status = status
        self.countdown = 0
        self.mqttParams = mqttParams
        self.armingCountdown = armingCountdown
        self.triggeredCountdown = triggeredCountdown
        self.mqttId = mqttId
        self.disarmPin = disarmPin
        self.sendPin = ''
        self.armedAway = armedAway
        self.armedHome = armedHome
        self.notification = notification
        self.armStatus = None   # need to store this in case alarm status changes to TRIGGERED or ACTIVATED

        signal.signal( signal.SIGINT, self.__signalHandler )
        
    def run( self ):
        #create a mqtt client
        self.client = mqtt.Client( self.mqttId )
        self.client.on_connect = self.__on_connect
        self.client.on_message = self.__on_message
        #set last will and testament before connecting
        self.client.will_set( self.mqttParams.publishTopic, Status( StatusMain.UNAVAILABLE ).toJson(), qos = 1, retain = True )
        self.client.connect( self.mqttParams.address, self.mqttParams.port )
        self.client.loop_start()
        #go in infinite loop
        while( True ):
            pass

    def __signalHandler( self, signal, frame ):
        print('Ctrl+C pressed!')
        self.client.disconnect()
        self.client.loop_stop()
        sys.exit(0)        

    def __on_connect( self, client, userdata, flags_dict, result ):
        """Executed when a connection with the mqtt broker has been established
        """
        #debug:
        m = "Connected flags"+str(flags_dict)+"result code " + str(result)+"client1_id  "+str(client)
        print( m )

        #subscribe to start listening for incomming commands
        self.client.subscribe( self.mqttParams.subscribeTopic )

        #publish the initial status
        self.__setStatus( self.status )

    def __on_message( self, client, userdata, message ):
        """Executed when an mqtt arrives
        """
        text = message.payload.decode( "utf-8" )
        print( 'Received message "{}"'.format( text ) )
        if( mqtt.topic_matches_sub( self.mqttParams.subscribeTopic, message.topic ) ):
            if( text.find( AlarmCommand.ARM_HOME.name ) > -1 ):
                self.__arm( Status( StatusMain.ARMED_HOME ) )
            elif( text.find( AlarmCommand.ARM_AWAY.name ) > -1 ):
                self.__arm( Status( StatusMain.ARMED_AWAY ) )
            elif( text.find( AlarmCommand.DEACTIVATE_REQUEST.name ) > -1 ):
                self.__deactivateRequest()
            elif( text.find( AlarmCommand.DEACTIVATE.name ) > -1 ):
                self.__deactivate( text )
            elif( text.find( AlarmCommand.DISARM.name ) > -1 ):
                self.__disarm()
            else:
                print( 'Unknown command: [{0}]'.format( text ) )
        else:
            # check for trigger when alarm in one of specific statuses
            if( self.status.main in [ StatusMain.ARMED_AWAY, StatusMain.ARMED_HOME ] ):
                triggers = []
                if( StatusMain.ARMED_AWAY == self.armStatus ):
                    triggers = self.armedAway.triggers
                elif( StatusMain.ARMED_HOME == self.armStatus ):
                    triggers = self.armedHome.triggers
                for t in triggers:
                    for o in t.topics: 
                        if( mqtt.topic_matches_sub( o, message.topic ) and re.search( t.regex, text ) is not None ):
                            self.__logTrigger( message )
                            self.__trigger( message, t.notify )                            
            elif( self.status.main in [ StatusMain.ACTIVATED, StatusMain.TRIGGERED ] ):
                #when already activated or triggered just log the trigger event
                self.__logTrigger( message )

    def __logActivation( self, message ):
        print( 'Alarm was ACTIVATED at [{}] by message <{}> "{}"'.format( datetime.now(), message.topic, message.payload.decode( "utf-8" ) ) )
    
    def __activate( self, message, notify ):
        self.__logActivation( message )
        self.__setStatus( Status( StatusMain.ACTIVATED ) )
        self.client.publish( self.notification.notifierMqttPublish, notify.toMqttCommand( self.notification.generateMessage( message ) ), qos = 2, retain = True )

    def __logTrigger( self, message ):
        print( 'Alarm was triggered at [{}] by message <{}> "{}"'.format( datetime.now(), message.topic, message.payload.decode( "utf-8" ) ) )

    def __trigger( self, message, notify ):
        if( self.status.main not in [ StatusMain.ARMED_AWAY, StatusMain.ARMED_HOME ] ):
            return
        print( 'Triggered!' )
        self.__setStatus( Status( StatusMain.TRIGGERED, self.triggeredCountdown, self.sendPin ) )
        self.__doTrigger( message, notify )

    def __doTrigger( self, message, notify ):
        """Will activate when triggerCountdown is finished
        """
        if( StatusMain.TRIGGERED != self.status.main ):
            print( '__trigger will stop here because status is {} instead of ACTIVATED. The alarm was probably deactivated'.format( self.status.main ) )
            return
        
        if( self.status.countdown > 0 ):
            threading.Timer(1.0, self.__doTrigger, [message, notify] ).start()
            print( '__doTrigger countdown {} to get to TRIGGERED'.format( self.status.countdown ) )
            self.__setStatus( Status( StatusMain.TRIGGERED, self.status.countdown -1, self.sendPin ) )
        else:
            print( '__doTrigger will set alarm to ACTIVATED because countdown has finished' )
            self.__activate( message, notify )
    
    def __arm( self, finalArmStatus ):
        """ Sets the status to arming and calls __doArm with the finalStatus
            to be set after countdown
        """
        if( StatusMain.UNARMED != self.status.main ):
            return

        self.__setStatus( Status( StatusMain.ARMING, self.armingCountdown ) );
        self.__doArm( finalArmStatus )
    
    def __doArm( self, finalArmStatus ):
        """ Will check countdown. If not 0, decrement and reschedule execution.
            Else set finalArmingStatus
        """
        if( StatusMain.ARMING != self.status.main ):
            print( '__doArm will stop here because status is {} instead of ARMING'.format( self.status.main ) )
            return

        if( self.status.countdown > 0 ):
            threading.Timer(1.0, self.__doArm, [ finalArmStatus ] ).start()
            print( '__doArm countdown {} to get to {}'.format( self.status.countdown, finalArmStatus.main ) )
            self.__setStatus( Status( StatusMain.ARMING, self.status.countdown -1 ) )
        else:
            print( '__doArm will set status to {} because countdown has finished'.format( finalArmStatus.main ) )

            #subscribe to trigger topics
            triggers = []
            mqttPublish = []
            if( StatusMain.ARMED_AWAY == finalArmStatus.main ):
                triggers = self.armedAway.triggers
                mqttPublish = self.armedAway.start
            elif( StatusMain.ARMED_HOME == finalArmStatus.main ):
                triggers = self.armedHome.triggers
                mqttPublish = self.armedHome.start
            for t in triggers:
                for o in t.topics:                    
                    print( '\tsubscribing to trigger topic: {}', o )
                    self.client.subscribe( o )  #subscribe to trigger topics
            for p in mqttPublish:
                self.client.publish( p.topic, p.command, qos = 2, retain = True )

            # set and publish new status
            self.armStatus = finalArmStatus.main
            self.__setStatus( finalArmStatus ) 

    def __deactivateRequest( self ):
        """ Generates a random pin and expects the mod 10 sum (per digit) with the disarmPin. When the answer comes will do the subtraction mod 10 and check against disarmPin
        """
        self.sendPin = ''
        for i in range( 4 ):
            self.sendPin += str( random.randint( 0, 9 ) )
        print( 'disarmPin: {}, challengePin:{}'.format( self.disarmPin, self.sendPin ) )
        self.__setStatus( Status( self.status.main, self.status.countdown, self.sendPin ) )

    def __deactivate( self, text ):
        print( 'Attempt to deactivate with text: "{}"'.format( text ) )
        if( self.status.main not in [ StatusMain.TRIGGERED, StatusMain.ACTIVATED ] ):
            print( 'Current status {}. Will not attempt deactivation.'.format( self.status.main.name ) )
            return
            
        response = None
        try:
            response = json.loads( text )
        except ValueError, e:
            print( '"{}" is not a valid json text, exiting.'.format( text ) )
            return
        
        if( 'pin' not in response ):
            print( 'pin not found in text {}. Will not deactivate'.format( text ) )
            return
        pin = response['pin']
        for i in range( 4 ):            
            if( not pin[i].isdigit() ):
                print( '{} is not a digit, deactivate exits'.format( text[i] ) )
                return

            if( str( ( int( pin[i] ) - int( self.sendPin[i] ) )%10 ) != self.disarmPin[i] ):
                print( 'Wrong pin! Will not deactivate' )
                return
            
        print( 'pin:{} == disarmPin:{}. Will deactivate '.format( text, self.disarmPin ) )
        self.sendPin = ''
        self.__doDisarm()

    def __disarm( self ):
        if( StatusMain.ARMED_AWAY == self.status.main or StatusMain.ARMED_HOME == self.status.main or StatusMain.ARMING == self.status.main ):
            self.__doDisarm()

    def __doDisarm( self ):
            print( 'Disarm will unsubscribe from trigger topics' )
            triggers = []
            mqttPublish = []
            if( StatusMain.ARMED_AWAY == self.status.main ):
                triggers = self.armedAway.triggers
                mqttPublish = self.armedAway.stop
            elif( StatusMain.ARMED_HOME == self.status.main ):
                triggers = self.armedHome.triggers
                mqttPublish = self.armedHome.stop
            for t in triggers:
                for o in t.topics:                    
                    print( '\tunsubscribing from trigger topic: {}', o )
                    self.client.unsubscribe( o )
            for p in mqttPublish:
                self.client.publish( p.topic, p.command, qos = 2, retain = True )

            self.armStatus = StatusMain.UNARMED
            self.__setStatus( Status( StatusMain.UNARMED ) )

    def __setStatus( self, status ):
        self.status = status
        self.client.publish( self.mqttParams.publishTopic, self.status.toJson(), qos = 2, retain = True )

if( __name__ == '__main__' ):
    configurationFile = 'alarm.conf'
    if( not os.path.isfile( configurationFile ) ):
        print( 'Configuration file "{}" not found, exiting.'.format( configurationFile ) )
        sys.exit()

    with open( configurationFile ) as json_file:
        configuration = json.load( json_file )
        print( 'Configuration: ' )
        pprint( configuration )

        alarm = MqttAlarm( 
            configuration['armingCountdown'], 
            configuration['triggeredCountdown'], 
            configuration['disarmPin'], 
            configuration['mqttId'], 
            MqttParams( configuration['mqttParams']['address'], int( configuration['mqttParams']['port'] ), configuration['mqttParams']['subscribeTopic'], configuration['mqttParams']['publishTopic'] ),
            ArmConfig( 
                [ MqttPublish( x['topic'], x['command'] ) for x in configuration['armedAway']['start'] ],
                [ MqttPublish( x['topic'], x['command'] ) for x in configuration['armedAway']['stop'] ],
                [ 
                    Trigger( 
                        x['topics'], 
                        x['regex'], 
                        None if 'notify' not in x else Notify( 
                            [] if 'sms' not in x['notify'] else x['notify']['sms'], 
                            [] if 'phonecall' not in x['notify'] else x['notify']['phonecall'], 
                            [] if 'im' not in x['notify'] else x['notify']['im'], 
                            None if 'email' not in x['notify'] else EmailConfig( x['notify']['email']['from'], x['notify']['email']['to'], x['notify']['email']['subject']  )
                        )
                    )
                    for x in configuration['armedAway']['triggers']
                ]
            ),
            ArmConfig( 
                [ MqttPublish( x['topic'], x['command'] ) for x in configuration['armedHome']['start'] ],
                [ MqttPublish( x['topic'], x['command'] ) for x in configuration['armedHome']['stop'] ],
                [ 
                    Trigger( 
                        x['topics'], 
                        x['regex'], 
                        None if 'notify' not in x else Notify( 
                            [] if 'sms' not in x['notify'] else x['notify']['sms'], 
                            [] if 'phonecall' not in x['notify'] else x['notify']['phonecall'], 
                            [] if 'im' not in x['notify'] else x['notify']['im'], 
                            None if 'email' not in x['notify'] else EmailConfig( x['notify']['email']['from'], x['notify']['email']['to'], x['notify']['email']['subject']  )
                        )
                    )
                    for x in configuration['armedHome']['triggers'] 
                ]
            ),
            Notification( configuration['notification']['notifierMqttPublish'], configuration['notification']['messageTemplate'], configuration['notification']['translationUrl'] ),
            Status( StatusMain[ configuration['status']['main'] ], configuration['status']['countdown'] )
        )
        alarm.run()