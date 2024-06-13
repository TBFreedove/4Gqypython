# -*- coding: utf-8 -*-

import app_fota
from misc import Power
import utime
import checkNet
import SecureData
import log
import net
import _thread
import dataCall
from umqtt import MQTTClient
import modem
from misc import ADC
from machine import Pin
import sim
from misc import Power
from machine import UART
from machine import I2C
import ujson
from machine import WDT
from machine import Timer

PROJECT_NAME = "QX_binhua"
PROJECT_VERSION = "1.240514"
ErrorMark=0                          # mark program executed success or not
refresh_time=bytearray(3)            # time interval to store   0-16777215s
time_set=1                           # time interval to sleep
TaskEnable = True
checknet = checkNet.CheckNetwork(PROJECT_NAME, PROJECT_VERSION)
gpio29 = Pin(Pin.GPIO29, Pin.OUT, Pin.PULL_PD, 0)
dicValue={"version":"1.0","simIccid":"0","temperature":"0","csq":"0","humidity":"0","refresh time":"60","battery voltage":0}
dicValue["version"]=PROJECT_VERSION
i2c_obj = I2C(I2C.I2C2, I2C.STANDARD_MODE)
dicReceive={}

INITCOMMAND = bytearray([0x08, 0])         # fist:  I2C init option
WHO_AM_I = bytearray([0x02, 0])            # sec:   send the adress of I2C register  
STARTMEASURE=bytearray([0x00, 0x00])       # third: measure order
READHUM=bytearray([0x01, 0x00])
humidityValue=bytearray(2)
temperatureValue= bytearray(2)


log.basicConfig(level=log.INFO)
mqtt_log = log.getLogger("MQTT")
i2c_log = log.getLogger("I2C")
uart_log = log.getLogger("UART")
adc_log = log.getLogger("ADC")
init_log=log.getLogger('INIT')
machine_log=log.getLogger('MACHINE')

# timer1 = Timer(Timer.Timer1)
# def feed(t):
#     wdt.feed()
#     machine_log.info('feed...')

class MqttClient():
    '''
    mqtt init
    '''

    # 说明：reconn该参数用于控制使用或关闭umqtt内部的重连机制，默认为True，使用内部重连机制。
    # 如需测试或使用外部重连机制可参考此示例代码，测试前需将reconn=False,否则默认会使用内部重连机制！
    def __init__(self, clientid, server, port, user=None, password=None, keepalive=0, ssl=False, ssl_params={},
                 reconn=True):
        self.__clientid = clientid
        self.__pw = password
        self.__server = server
        self.__port = port
        self.__uasename = user
        self.__keepalive = keepalive
        self.__ssl = ssl
        self.__ssl_params = ssl_params
        self.topic = None
        self.qos = None
        # 网络状态标志
        self.__nw_flag = True
        # 创建互斥锁
        self.mp_lock = _thread.allocate_lock()
        # 创建类的时候初始化出mqtt对象
        self.client = MQTTClient(self.__clientid, self.__server, self.__port, self.__uasename, self.__pw,
                                 keepalive=self.__keepalive, ssl=self.__ssl, ssl_params=self.__ssl_params,
                                 reconn=reconn)

    def connect(self):
        '''
        连接mqtt Server
        '''
        self.client.connect()
        # 注册网络回调函数，网络状态发生变化时触发
        flag = dataCall.setCallback(self.nw_cb)
        if flag != 0:
            # 回调注册失败
            raise Exception("Network callback registration failed")

    def set_callback(self, sub_cb):
        '''
        设置mqtt回调消息函数
        '''
        self.client.set_callback(sub_cb)

    def error_register_cb(self, func):
        '''
        注册一个接收umqtt内线程异常的回调函数
        '''
        self.client.error_register_cb(func)

    def subscribe(self, topic, qos=0):
        '''
        订阅Topic
        '''
        self.topic = topic  # 保存topic ，多个topic可使用list保存
        self.qos = qos  # 保存qos
        self.client.subscribe(topic, qos)

    def publish(self, topic, msg, qos=0):
        '''
        发布消息
        '''
        self.client.publish(topic, msg, qos)

    def disconnect(self):
        '''
        关闭连接
        '''
        global TaskEnable
        # 关闭wait_msg的监听线程
        TaskEnable = False
        # 关闭之前的连接，释放资源
        self.client.disconnect()

    def reconnect(self):
        '''
        mqtt 重连机制(该示例仅提供mqtt重连参考，根据实际情况调整)
        PS：1.如有其他业务需要在mqtt重连后重新开启，请先考虑是否需要释放之前业务上的资源再进行业务重启
            2.该部分需要自己根据实际业务逻辑添加，此示例只包含mqtt重连后重新订阅Topic
        '''
        # 判断锁是否已经被获取
        if self.mp_lock.locked():
            return
        self.mp_lock.acquire()
        # 重新连接前关闭之前的连接，释放资源(注意区别disconnect方法，close只释放socket资源，disconnect包含mqtt线程等资源)
        self.client.close()
        # 重新建立mqtt连接
        while True:
            net_sta = net.getState()  # 获取网络注册信息
            if net_sta != -1 and net_sta[1][0] == 1:
                call_state = dataCall.getInfo(1, 0)  # 获取拨号信息
                if (call_state != -1) and (call_state[2][0] == 1):
                    try:
                        # 网络正常，重新连接mqtt
                        self.connect()
                    except Exception as e:
                        # 重连mqtt失败, 5s继续尝试下一次
                        self.client.close()
                        utime.sleep(5)
                        continue
                else:
                    # 网络未恢复，等待恢复
                    utime.sleep(10)
                    continue
                # 重新连接mqtt成功，订阅Topic
                try:
                    # 多个topic采用list保存，遍历list重新订阅
                    if self.topic is not None:
                        self.client.subscribe(self.topic, self.qos)
                    self.mp_lock.release()
                except:
                    # 订阅失败，重新执行重连逻辑
                    self.client.close()
                    utime.sleep(5)
                    continue
            else:
                utime.sleep(5)
                continue
            break  # 结束循环
        # 退出重连
        return True

    def nw_cb(self, args):
        '''
        dataCall 网络回调
        '''
        nw_sta = args[1]
        if nw_sta == 1:
            # 网络连接
            mqtt_log.info("*** network connected! ***")
            self.__nw_flag = True
        else:
            # 网络断线
            mqtt_log.info("*** network not connected! ***")
            self.__nw_flag = False

    def __listen(self):
        while True:
            try:
                if not TaskEnable:
                    break
                self.client.wait_msg()
            except OSError as e:
                # 判断网络是否断线
                if not self.__nw_flag:
                    # 网络断线等待恢复进行重连
                    self.reconnect()
                # 在socket状态异常情况下进行重连
                elif self.client.get_mqttsta() != 0 and TaskEnable:
                    self.reconnect()
                else:
                    # 这里可选择使用raise主动抛出异常或者返回-1
                    return -1
    
    def GetValue(self):  
        global time_set  
        adc = ADC()
        uart0 = UART(UART.UART0, 9600, 8, 0, 1, 0)
        uart1 = UART(UART.UART1, 9600, 8, 0, 1, 0)
        uart2 = UART(UART.UART2, 9600, 8, 0, 1, 0)       

        while 1:
            temptime=(utime.time())
            gpio29.write(1)  
            adc.open()                                       # peripherals power on
            utime.sleep(5) 


            dicValue["battery voltage"]=Power.getVbatt()  
            dicValue["refresh time"]=time_set 

            i2c_obj.write(0x40, WHO_AM_I, 1, INITCOMMAND, 2)
            utime.sleep_ms(300)
            i2c_obj.write(0x40, STARTMEASURE, 1, INITCOMMAND, 0)
            utime.sleep_ms(300)
            i2c_obj.read(0x40, STARTMEASURE, 1, temperatureValue, 2, 50)   # read     
            i2c_obj.read(0x40, READHUM, 1, humidityValue, 2, 50)   # read   
            tempa=(temperatureValue[0]*255+temperatureValue[1])*165/65536-40 
            tempb=(humidityValue[0]*255+humidityValue[1])/65536*100
            # dicValue["temperature"]=round(tempa,2)
            dicValue["temperature"]='{:.2f}'.format(tempa)
            # dicValue["humidity"]=round(tempb,2)
            dicValue["humidity"]='{:.2f}'.format(tempb)

            tempadc0=adc.read(ADC.ADC0)
            tempadc1=adc.read(ADC.ADC1)
            if tempadc0>200:
                if tempadc0<288:
                    dicValue["wind direction"]="北风"  
                elif tempadc0<416:
                    dicValue["wind direction"]="东北风"
                elif tempadc0<543:
                    dicValue["wind direction"]="东风"  
                elif tempadc0<672:
                    dicValue["wind direction"]="东南风"   
                elif tempadc0<800:
                    dicValue["wind direction"]="南风"   
                elif tempadc0<928:
                    dicValue["wind direction"]="西南风"   
                elif tempadc0<1055:
                    dicValue["wind direction"]="西风"  
                else:
                    dicValue["wind direction"]="西北风"                   
            if tempadc1>200:    
                if tempadc1<240:
                    dicValue["wind speed"]='0m/s'
                else:
                    b=(tempadc1-240)/29.866
                    dicValue["wind speed"]='{:.2f}m/s'.format(b)

            uart0.write("A")                                           #uart get value 
            uart1.write("A")
            uart2.write("A")
            utime.sleep_ms(100)
            tempmsg=uart0.read(uart0.any())
            if len(tempmsg)>3 :
                tempstr=tempmsg.decode()
                tempValue=tempstr.split(":",2)
                tempsec=tempValue[1].split(",",3)
                if tempsec[0]=="VOC" or tempsec[0]=="AQI":          
                    dicValue["VOC"]=tempsec[1]
                elif tempsec[0]=="H2S":
                    dicValue["H2S"]=tempsec[1]
                elif tempsec[0]=="SO2":
                    dicValue["SO2"]=tempsec[1]
            tempmsg=uart1.read(uart1.any())
            if len(tempmsg)>3 :
                tempstr=tempmsg.decode()
                tempValue=tempstr.split(":",2)
                tempsec=tempValue[1].split(",",3)
                if tempsec[0]=="VOC" or tempsec[0]=="AQI":
                    dicValue["VOC"]=tempsec[1]
                elif tempsec[0]=="H2S":
                    dicValue["H2S"]=tempsec[1]
                elif tempsec[0]=="SO2":
                    dicValue["SO2"]=tempsec[1]
            tempmsg=uart2.read(uart2.any())
            if len(tempmsg)>3 :
                tempstr=tempmsg.decode()
                tempValue=tempstr.split(":",2)
                tempsec=tempValue[1].split(",",3)
                if tempsec[0]=="VOC" or tempsec[0]=="AQI":
                    dicValue["VOC"]=tempsec[1]
                elif tempsec[0]=="H2S":
                    dicValue["H2S"]=tempsec[1]
                elif tempsec[0]=="SO2":
                    dicValue["SO2"]=tempsec[1]

            gpio29.write(0)                                                   # peripherals power off before sleep  
            c.publish(modem.getDevImei(), ujson.dumps(dicValue))  
            if 'VOC'in dicValue:
                del dicValue["VOC"]                                               # when sensor off del the key
            if 'H2S'in dicValue:
                del dicValue["H2S"]
            if 'SO2'in dicValue:
                del dicValue["SO2"]
            if 'wind direction'in dicValue:
                del dicValue["wind direction"]
            if 'wind speed'in dicValue:
                del dicValue["wind speed"]
            while 1:
                if utime.time()>=(temptime+time_set) :
                    break                                                                            
    def loop_forever(self):
         _thread.start_new_thread(self.__listen, ())




if __name__ == '__main__':

    utime.sleep(5)
    checknet.poweron_print_once()    
    checknet.wait_network_connected()

    # wdt = WDT(20)  # 启动看门狗，设置超时时间
    # timer1.start(period=15000, mode=timer1.PERIODIC, callback=feed)  # 使用定时器喂狗


    def sub_cb(topic, msg):
        global dicReceive
        global time_set
        dicReceive=ujson.loads(msg.decode()) 
        mqtt_log.info(dicReceive)
        if 'reboot' in dicReceive:
            if dicReceive['reboot']=='True':
                Power.powerRestart()

        if 'refresh time' in dicReceive:
            if dicReceive['refresh time']<16777215:
                time_set=dicReceive["refresh time"]
                refresh_time[0]=time_set//65536
                refresh_time[1]=(time_set%65536)//256
                refresh_time[2]=time_set%256
                SecureData.Store(1,refresh_time,3)

        if "version" in dicReceive:    
            if dicReceive["version"]!=PROJECT_VERSION :
                mqtt_log.info("helo form fota")
                fota = app_fota.new()
                fota.download('http://39.106.51.50:8000/main.py','/usr/main.py')
                fota.set_update_flag()
                mqtt_log.info('reboot...')
                utime.sleep(1)
                Power.powerRestart()

    def err_cb(error):
        '''
        接收umqtt线程内异常的回调函数
        '''
        mqtt_log.info(error)
        c.reconnect() # 可根据异常进行重连
    init_log.info(modem.getDevImei())
    dicValue["simIccid"]=sim.getIccid()  #get simIccid onece    
    dicValue["csq"]=net.csqQueryPoll()

    SecureData.Read(1,refresh_time,3)
    time_set=refresh_time[0]*65536+refresh_time[1]*256+refresh_time[2]
    if time_set==0:
        time_set=120     

    c = MqttClient(modem.getDevImei(), "39.106.51.50", 1883, reconn=False)
    c.set_callback(sub_cb)
    c.error_register_cb(err_cb)
    c.connect()
    c.subscribe(modem.getDevImei())
    c.loop_forever()
    c.GetValue()



